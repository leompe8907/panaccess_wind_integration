import logging
import secrets
from datetime import timedelta
import base64
import json

from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.conf import settings
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from wind.functions.getSubscriberLoginInfo import CallGetSubscriberLoginInfo

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated

from wind.models import UDIDAuthRequest, SubscriberInfo, AppCredentials, EncryptedCredentialsLog
from wind.serializers import UDIDAssociationSerializer
from wind.services.udid_auth_service import authenticate_with_udid_service, compute_encrypted_hash
from wind.utils.websocket_utils import (
    get_client_ip,
    generate_device_fingerprint,
    check_device_fingerprint_rate_limit,
    increment_rate_limit_counter,
    get_client_token,
    check_token_bucket_lua,
    check_udid_rate_limit,
    is_legitimate_reconnection,
    should_apply_retry_delay,
    check_adaptive_rate_limit,
    reset_retry_info,
    get_retry_info,
    is_valid_app_type
)
from wind.utils.crypto_tv import hybrid_encrypt_for_app
from wind.utils.log_buffer import log_audit_async

logger = logging.getLogger(__name__)


def get_cached_app_credentials(app_type, app_version):
    """
    Devuelve AppCredentials desde cache de corto plazo para reducir
    consultas a BD bajo alta concurrencia.
    """
    cache_key = f"appcred:{app_type}:{app_version}"
    app_credentials = cache.get(cache_key)
    if app_credentials is not None:
        return app_credentials

    app_credentials = AppCredentials.objects.filter(
        app_type=app_type,
        app_version=app_version,
        is_active=True
    ).first()

    cache.set(cache_key, app_credentials, timeout=10)
    return app_credentials


class RequestUDIDManualView(APIView):
    permission_classes = [AllowAny]
    
    def get(self, request):
        """
        Generar UDID único para solicitud manual (Smart TV).
        """
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        
        logger.info(
            f"RequestUDIDManualView: Request recibido - ip={client_ip}, user_agent={user_agent[:100] if user_agent else 'N/A'}"
        )
        
        try:
            device_fingerprint = generate_device_fingerprint(request)
            
            is_allowed, remaining, retry_after = check_device_fingerprint_rate_limit(
                device_fingerprint,
                max_requests=1,
                window_minutes=5
            )
            
            if not is_allowed:
                logger.warning(
                    f"RequestUDIDManualView: Rate limit excedido - device_fingerprint={device_fingerprint[:8]}..., ip={client_ip}, retry_after={retry_after}s"
                )
                retry_at = timezone.now() + timedelta(seconds=retry_after)
                return Response({
                    "error_code": "DEVICE_FP_RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_after,
                    "retry_at": retry_at.isoformat(),
                    "remaining_requests": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                    "Retry-After": str(retry_after)
                })

            udid = self.generate_unique_udid()
            
            auth_request = UDIDAuthRequest.objects.create(
                udid=udid,
                status='pending',
                client_ip=client_ip,
                user_agent=user_agent,
                device_fingerprint=device_fingerprint
            )
            
            auth_request.refresh_from_db()
            increment_rate_limit_counter('device_fp', device_fingerprint)
            
            log_audit_async(
                action_type='udid_generated',
                udid=udid,
                client_ip=client_ip,
                user_agent=user_agent,
                details={
                    'method': 'manual_request',
                    'device_fingerprint': device_fingerprint,
                    'device_fingerprint_stored': auth_request.device_fingerprint,
                    'rate_limit_remaining': remaining - 1
                }
            )
            
            logger.info(
                f"RequestUDIDManualView: UDID generado exitosamente - udid={udid}, device_fingerprint={device_fingerprint[:8]}..., ip={client_ip}"
            )
            
            return Response({
                "udid": auth_request.udid,
                "expires_at": auth_request.expires_at,
                "status": auth_request.status,
                "expires_in_minutes": 5,
                "device_fingerprint": auth_request.device_fingerprint,
                "remaining_requests": remaining - 1,
                "rate_limit": {
                    "remaining": remaining - 1,
                    "reset_in_seconds": 5 * 60
                }
            }, status=status.HTTP_201_CREATED)
            
        except Exception as e:
            logger.error(f"RequestUDIDManualView: Error interno - ip={client_ip}, error={str(e)}", exc_info=True)
            return Response({"error": "Internal server error"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def generate_unique_udid(self):
        while True:
            udid = secrets.token_hex(4)
            if not UDIDAuthRequest.objects.filter(udid=udid).exists():
                return udid


class ValidateAndAssociateUDIDView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Valida y asocia un UDID con un suscriptor.
        """
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')
        
        logger.info(
            f"ValidateAndAssociateUDIDView: Request recibido - ip={client_ip}"
        )
        
        client_token = get_client_token(request)
        if client_token:
            is_allowed, remaining, retry_after = check_token_bucket_lua(
                identifier=client_token,
                capacity=5,
                refill_rate=1,
                window_seconds=60,
                tokens_requested=1
            )
            
            if not is_allowed:
                logger.warning(
                    f"ValidateAndAssociateUDIDView: Rate limit excedido - token={client_token[:8]}..., ip={client_ip}, retry_after={retry_after}s"
                )
                retry_at = timezone.now() + timedelta(seconds=retry_after)
                return Response({
                    "error_code": "RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_after,
                    "retry_at": retry_at.isoformat(),
                    "remaining": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                    "Retry-After": str(retry_after)
                })
        
        serializer = UDIDAssociationSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(f"ValidateAndAssociateUDIDView: Datos inválidos - ip={client_ip}, errors={serializer.errors}")
            return Response({"errors": serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        udid_request = data["udid_request"]
        udid = udid_request.udid if hasattr(udid_request, 'udid') else None
        
        if udid:
            is_allowed, remaining, retry_after = check_udid_rate_limit(
                udid,
                max_requests=1,
                window_minutes=1
            )
            
            if not is_allowed:
                logger.warning(
                    f"ValidateAndAssociateUDIDView: Rate limit por UDID excedido - udid={udid}, ip={client_ip}"
                )
                retry_at = timezone.now() + timedelta(seconds=retry_after)
                return Response({
                    "error_code": "UDID_ASSOCIATION_RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_after,
                    "retry_at": retry_at.isoformat(),
                    "remaining_requests": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                    "Retry-After": str(retry_after)
                })
        
        subscriber = data["subscriber"]
        sn = data["sn"]
        operator_id = data["operator_id"]
        method = data["method"]

        with transaction.atomic():
            udid_request = UDIDAuthRequest.objects.select_for_update().get(pk=udid_request.pk)
            udid = udid_request.udid

            self.associate_udid_with_subscriber(
                udid_request, subscriber, sn, operator_id, method, request
            )

            def _notify():
                try:
                    channel_layer = get_channel_layer()
                    if channel_layer:
                        async_to_sync(channel_layer.group_send)(
                            f"udid_{udid}",
                            {"type": "udid.validated", "udid": udid}
                        )
                        logger.info("Notificado udid.validated para %s", udid)
                    else:
                        logger.warning("Channel layer no disponible; no se notificó udid %s", udid)
                except Exception as e:
                    logger.exception("Error notificando WebSocket para udid %s: %s", udid, e)

            transaction.on_commit(_notify)

        logger.info(
            f"ValidateAndAssociateUDIDView: Asociación exitosa - udid={udid_request.udid}, subscriber_code={subscriber.subscriber_code}, sn={sn}, ip={client_ip}"
        )
        
        if udid:
            increment_rate_limit_counter('udid', udid)

        response_data = {
            "message": "UDID validated and associated successfully",
            "udid": udid_request.udid,
            "subscriber_code": subscriber.subscriber_code,
            "smartcard_sn": sn,
            "status": udid_request.status,
            "validated_at": udid_request.validated_at,
            "used_at": udid_request.used_at,
            "validated_by_operator": operator_id
        }
        
        if udid and remaining is not None:
            response_data["remaining_requests"] = remaining - 1
            response_data["rate_limit"] = {
                "remaining": remaining - 1,
                "reset_in_seconds": 60
            }

        return Response(response_data, status=status.HTTP_200_OK)

    def associate_udid_with_subscriber(self, auth_request, subscriber, sn, operator_id, method, request):
        now = timezone.now()
        client_ip = get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")

        auth_request.subscriber_code = subscriber.subscriber_code
        auth_request.sn = sn
        auth_request.status = "validated"
        auth_request.validated_at = now
        auth_request.used_at = now
        auth_request.validated_by_operator = operator_id
        auth_request.client_ip = client_ip
        auth_request.user_agent = user_agent
        auth_request.method = method
        auth_request.save()

        subscriber.last_login = now
        subscriber.save(update_fields=["last_login"])

        log_audit_async(
            action_type="udid_used",
            udid=auth_request.udid,
            subscriber_code=subscriber.subscriber_code,
            operator_id=operator_id,
            client_ip=client_ip,
            user_agent=user_agent,
            details={
                "subscriber_name": f"{subscriber.first_name} {subscriber.last_name}".strip(),
                "smartcard_sn": sn,
                "validation_timestamp": now.isoformat(),
            },
        )


class AuthenticateWithUDIDView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Autentica con UDID y entrega credenciales cifradas (híbrido).
        """
        udid = request.data.get('udid')
        app_type = request.data.get('app_type', 'android_tv')
        app_version = request.data.get('app_version', '1.0')
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')

        logger.info(
            f"AuthenticateWithUDIDView: Request recibido - udid={udid[:8] if udid else 'N/A'}..., app_type={app_type}, ip={client_ip}"
        )

        if not udid:
            return Response({"error": "UDID is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        if not is_valid_app_type(app_type):
            return Response({
                "error": "Invalid app_type. Must be: android_tv, samsung_tv, lg_tv, set_top_box, mobile_app, web_player"
            }, status=status.HTTP_400_BAD_REQUEST)

        client_token = get_client_token(request)
        if client_token:
            is_allowed, remaining, retry_after = check_token_bucket_lua(
                identifier=client_token,
                capacity=5,
                refill_rate=1,
                window_seconds=60,
                tokens_requested=1
            )
            
            if not is_allowed:
                retry_at = timezone.now() + timedelta(seconds=retry_after)
                return Response({
                    "error_code": "RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_after,
                    "retry_at": retry_at.isoformat(),
                    "remaining": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                    "Retry-After": str(retry_after)
                })

        is_allowed, remaining, retry_after = check_udid_rate_limit(
            udid,
            max_requests=5,
            window_minutes=5
        )
        
        if not is_allowed:
            retry_at = timezone.now() + timedelta(seconds=retry_after)
            return Response({
                "error_code": "UDID_AUTH_RATE_LIMIT_EXCEEDED",
                "retry_after": retry_after,
                "retry_at": retry_at.isoformat(),
                "remaining_requests": remaining
            }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                "Retry-After": str(retry_after)
            })

        is_reconnection = is_legitimate_reconnection(udid)
        should_delay, retry_delay, attempt_number = should_apply_retry_delay(udid, action_type='reconnection')
        
        if should_delay and retry_delay > 0:
            retry_at = timezone.now() + timedelta(seconds=retry_delay)
            return Response({
                "error_code": "SERVICE_TEMPORARILY_UNAVAILABLE",
                "retry_after": retry_delay,
                "retry_at": retry_at.isoformat(),
                "attempt": attempt_number,
                "is_reconnection": is_reconnection
            }, status=status.HTTP_503_SERVICE_UNAVAILABLE, headers={
                "Retry-After": str(retry_delay)
            })

        if is_reconnection:
            is_allowed, remaining, retry_after, reason = check_adaptive_rate_limit('udid', udid, is_reconnection=True)
            if not is_allowed:
                retry_delay, _ = get_retry_info(udid, 'reconnection')
                retry_at = timezone.now() + timedelta(seconds=retry_delay)
                return Response({
                    "error_code": "RECONNECTION_RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_delay,
                    "retry_at": retry_at.isoformat(),
                    "is_reconnection": True,
                    "reason": reason
                }, status=status.HTTP_429_TOO_MANY_REQUESTS, headers={
                    "Retry-After": str(retry_delay)
                })

        try:
            with transaction.atomic():
                try:
                    req = UDIDAuthRequest.objects.select_for_update().get(udid=udid)
                except UDIDAuthRequest.DoesNotExist:
                    return Response({"error": "Invalid UDID"}, status=status.HTTP_404_NOT_FOUND)

                if req.status != 'validated':
                    return Response({"error": f"UDID not valid. Status: {req.status}"}, status=status.HTTP_403_FORBIDDEN)

                if req.is_expired():
                    req.status = 'expired'
                    req.save()
                    return Response({"error": "UDID has expired"}, status=status.HTTP_403_FORBIDDEN)

                try:
                    subscriber = SubscriberInfo.objects.get(subscriber_code=req.subscriber_code, sn=req.sn)
                except SubscriberInfo.DoesNotExist:
                    return Response({"error": "Subscriber info not found or mismatched SN"}, status=status.HTTP_404_NOT_FOUND)

                credentials_payload = {
                    "subscriber_code": subscriber.subscriber_code,
                    "sn": subscriber.sn,
                    "login1": subscriber.login1,
                    "login2": subscriber.login2,
                    "password": subscriber.get_password(),
                    "pin": subscriber.get_pin(),
                    "packages": subscriber.packages,
                    "products": subscriber.products,
                    "timestamp": timezone.now().isoformat()
                }

                app_credentials = get_cached_app_credentials(app_type, app_version)
                if not app_credentials:
                    return Response({
                        "error": f"No valid app credentials available for app_type='{app_type}'"
                    }, status=status.HTTP_503_SERVICE_UNAVAILABLE)

                try:
                    encrypted_result = hybrid_encrypt_for_app(
                        json.serialize_credentials(credentials_payload), app_type
                    )
                except Exception as e:
                    return Response({"error": "Encryption failed", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

                req.app_type = app_type
                req.app_version = app_version
                req.app_credentials_used = app_credentials
                req.mark_credentials_delivered(app_credentials)
                req.mark_as_used()

                log_audit_async(
                    action_type='udid_used',
                    udid=req.udid,
                    subscriber_code=req.subscriber_code,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    details={
                        "sn_assigned": subscriber.sn,
                        "app_type": app_type,
                        "app_version": app_version,
                        "encryption_method": "Hybrid AES-256 + RSA-OAEP",
                        "key_fingerprint": app_credentials.key_fingerprint
                    }
                )

                encrypted_hash = compute_encrypted_hash(encrypted_result['encrypted_data'])
                EncryptedCredentialsLog.objects.create(
                    udid=req.udid,
                    subscriber_code=req.subscriber_code,
                    sn=req.sn,
                    app_type=app_type,
                    app_version=app_version,
                    app_credentials_id=app_credentials,
                    encrypted_data_hash=encrypted_hash,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    delivered_successfully=True
                )
                
                increment_rate_limit_counter('udid', udid)
                if is_reconnection:
                    reset_retry_info(udid, 'reconnection')

                return Response({
                    "encrypted_credentials": encrypted_result,
                    "security_info": {
                        "encryption_method": "Hybrid AES-256 + RSA-OAEP",
                        "app_type": app_type,
                        "app_version": app_credentials.app_version
                    },
                    "expires_at": req.expires_at,
                    "remaining_requests": remaining - 1,
                    "rate_limit": {
                        "remaining": remaining - 1,
                        "reset_in_seconds": 5 * 60
                    }
                }, status=status.HTTP_200_OK)

        except Exception as e:
            if is_reconnection:
                get_retry_info(udid, 'reconnection')
            logger.error(f"AuthenticateWithUDIDView: Error interno - error={str(e)}", exc_info=True)
            return Response({"error": "Internal server error", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class ValidateStatusUDIDView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        """
        Valida el estado de un UDID (polling de respaldo).
        """
        udid = request.query_params.get('udid') or request.META.get('HTTP_X_UDID')
        client_ip = get_client_ip(request)
        user_agent = request.META.get('HTTP_USER_AGENT', '')

        if not udid:
            return Response({"error": "UDID is required"}, status=status.HTTP_400_BAD_REQUEST)

        client_token = get_client_token(request)
        if client_token:
            is_allowed, remaining, retry_after = check_token_bucket_lua(
                identifier=client_token,
                capacity=5,
                refill_rate=1,
                window_seconds=60,
                tokens_requested=1
            )
            if not is_allowed:
                retry_at = timezone.now() + timedelta(seconds=retry_after)
                return Response({
                    "error_code": "RATE_LIMIT_EXCEEDED",
                    "retry_after": retry_after,
                    "retry_at": retry_at.isoformat(),
                    "remaining": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        
        is_allowed, remaining, retry_after = check_udid_rate_limit(
            udid,
            max_requests=5,
            window_minutes=5
        )
        if not is_allowed:
            retry_at = timezone.now() + timedelta(seconds=retry_after)
            return Response({
                "error_code": "UDID_STATUS_RATE_LIMIT_EXCEEDED",
                "retry_after": retry_after,
                "retry_at": retry_at.isoformat()
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            req = UDIDAuthRequest.objects.get(udid=udid)
        except UDIDAuthRequest.DoesNotExist:
            log_audit_async(
                action_type='udid_validated',
                udid=udid,
                client_ip=client_ip,
                user_agent=user_agent,
                details={'error': 'UDID not found'}
            )
            return Response({"error": "Invalid UDID"})

        if req.status == 'revoked':
            log_audit_async(
                action_type='udid_validated',
                subscriber_code=req.subscriber_code,
                udid=udid,
                client_ip=client_ip,
                user_agent=user_agent,
                details={'error': 'UDID revoked'}
            )
            return Response({
                "error": "UDID has been revoked",
                "status": "revoked"
            }, status=status.HTTP_202_ACCEPTED)

        if req.is_expired():
            if req.status != 'expired':
                req.status = 'expired'
                req.save()
            
            log_audit_async(
                action_type='udid_validated',
                subscriber_code=req.subscriber_code,
                udid=udid,
                client_ip=client_ip,
                user_agent=user_agent,
                details={'error': 'UDID expired'}
            )
            return Response({
                "error": "UDID has expired",
                "status": "expired"
            }, status=status.HTTP_410_GONE)

        expiration_info = req.get_expiration_info()
        
        response_data = {
            "udid": udid,
            "status": req.status,
            "subscriber_code": req.subscriber_code,
            "sn": req.sn,
            "expiration": expiration_info
        }
        
        if req.status in ['validated', 'used']:
            response_data["valid"] = True
        elif req.status == 'pending':
            response_data["valid"] = req.is_valid()

        if req.status == 'validated':
            response_data.update({
                "validated_at": req.validated_at,
                "validated_by": req.validated_by_operator
            })
        elif req.status == 'used':
            response_data.update({
                "used_at": req.used_at,
                "credentials_delivered": req.credentials_delivered
            })
        elif req.status == 'pending':
            if expiration_info.get('time_remaining'):
                response_data["time_remaining_seconds"] = int(
                    expiration_info['time_remaining'].total_seconds()
                )

        log_audit_async(
            action_type='udid_validated',
            subscriber_code=req.subscriber_code,
            udid=udid,
            client_ip=client_ip,
            user_agent=user_agent,
            details={
                'status': req.status,
                'validation_successful': True
            }
        )

        if req.status == 'pending':
            req.attempts_count += 1
            req.save()

        return Response(response_data, status=status.HTTP_200_OK)


class DisassociateUDIDView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Desvincula un SN de un UDID específico.
        """
        udid = request.data.get('udid')
        operator_id = request.data.get('operator_id')
        reason = request.data.get('reason', 'Voluntary disassociation')
        client_ip = get_client_ip(request)

        if not udid:
            return Response({"error": "UDID is required"}, status=status.HTTP_400_BAD_REQUEST)

        client_token = get_client_token(request)
        if client_token:
            is_allowed, remaining, retry_after = check_token_bucket_lua(
                identifier=client_token,
                capacity=5,
                refill_rate=1,
                window_seconds=60,
                tokens_requested=1
            )
            if not is_allowed:
                return Response({
                    "error": "Rate limit exceeded",
                    "message": "Too many requests. Please retry later.",
                    "retry_after": retry_after,
                    "remaining": remaining
                }, status=status.HTTP_429_TOO_MANY_REQUESTS)
        
        is_allowed, remaining, retry_after = check_udid_rate_limit(
            udid,
            max_requests=5,
            window_minutes=60
        )
        if not is_allowed:
            return Response({
                "error": "Rate limit exceeded",
                "message": "Too many disassociation attempts for this UDID. Please try again later.",
                "retry_after": retry_after,
                "remaining_requests": remaining
            }, status=status.HTTP_429_TOO_MANY_REQUESTS)

        try:
            with transaction.atomic():
                try:
                    req = UDIDAuthRequest.objects.select_for_update().get(udid=udid)
                except UDIDAuthRequest.DoesNotExist:
                    return Response({"error": "UDID not found"}, status=status.HTTP_404_NOT_FOUND)

                if req.status not in ['validated', 'used', 'expired']:
                    return Response({
                        "error": f"Cannot disassociate: UDID is in state '{req.status}'"
                    }, status=status.HTTP_400_BAD_REQUEST)

                if not req.sn:
                    return Response({
                        "error": "No SN is currently associated with this UDID"
                    }, status=status.HTTP_400_BAD_REQUEST)

                old_sn = req.sn
                old_status = req.status

                req.sn = None
                req.status = 'revoked'
                req.revoked_at = timezone.now()
                req.revoked_reason = reason
                req.save()

                log_audit_async(
                    action_type='udid_revoked',
                    udid=req.udid,
                    subscriber_code=req.subscriber_code,
                    operator_id=operator_id,
                    client_ip=client_ip,
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                    details={
                        "old_sn": old_sn,
                        "old_status": old_status,
                        "revoked_at": timezone.now().isoformat(),
                        "reason": reason
                    }
                )

                return Response({
                    "message": f"UDID {req.udid} was successfully disassociated",
                    "revoked_at": req.revoked_at,
                    "subscriber_code": req.subscriber_code,
                }, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"DisassociateUDIDView: Error interno - error={str(e)}", exc_info=True)
            return Response({"error": "Internal server error", "details": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


def login_page_view(request):
    """
    Página principal de acceso: email/contraseña, registro y login social.
    """
    google_client_id = settings.SOCIALACCOUNT_PROVIDERS.get('google', {}).get('APP', {}).get('client_id', '')
    facebook_app_id = settings.SOCIALACCOUNT_PROVIDERS.get('facebook', {}).get('APP', {}).get('client_id', '')
    return render(
        request,
        'wind/login.html',
        {
            'google_client_id': google_client_id,
            'facebook_app_id': facebook_app_id,
        },
    )


def dashboard_view(request):
    """Área de usuario autenticado (JWT en el navegador)."""
    return render(request, "wind/dashboard.html")


def subscriber_test_view(request):
    """Página de prueba: solo muestra subscriber de /api/v1/profile/me/ con logs."""
    return render(request, "wind/subscriber_test.html")


def login_test_view(request):
    """
    Página de prueba para iniciar sesión con Google.
    Muestra un botón que enlaza al flujo de allauth (Google).
    """
    google_client_id = settings.SOCIALACCOUNT_PROVIDERS.get('google', {}).get('APP', {}).get('client_id', '')
    context = {
        'google_client_id': google_client_id,
    }
    return render(request, 'wind/login_test.html', context)


def login_facebook_test_view(request):
    """
    Página de prueba para iniciar sesión con Facebook (SDK JS) y consumir el endpoint REST.
    """
    facebook_app_id = settings.SOCIALACCOUNT_PROVIDERS.get('facebook', {}).get('APP', {}).get('client_id', '')
    context = {
        'facebook_app_id': facebook_app_id,
    }
    return render(request, 'wind/login_test_facebook.html', context)


@ensure_csrf_cookie
def register_view(request):
    """
    Página web para registrar suscriptores vía /wind/create-subscriber/.
    Se renderiza en el mismo origen para evitar CORS.
    """
    return render(request, 'wind/register.html', {'debug': bool(settings.DEBUG)})


def credentials_view(request):
    """
    Página web para mostrar credenciales PanAccess recién creadas.
    Se accede vía token firmado y expirable (?t=...).
    """
    token = request.GET.get("t", "")
    if not token:
        return render(request, "wind/credentials.html", {"error": "Enlace inválido o incompleto."}, status=400)

    signer = TimestampSigner(salt="wind.credentials")
    try:
        raw = signer.unsign(token, max_age=10 * 60)  # 10 minutos
    except SignatureExpired:
        return render(request, "wind/credentials.html", {"error": "Este enlace expiró. Regístrate de nuevo o solicita recuperación."}, status=400)
    except BadSignature:
        return render(request, "wind/credentials.html", {"error": "Enlace inválido."}, status=400)

    # Formato esperado:
    # "<subscriber_code>|<license_ok_int>|<b64(license_error)>|<b64(email)>"
    parts = str(raw).split("|")
    subscriber_code = parts[0] if parts else ""
    license_ok = parts[1] if len(parts) > 1 else ""
    license_err = ""
    email_from_token = ""

    if len(parts) >= 4:
        try:
            license_err = base64.urlsafe_b64decode(parts[2].encode("ascii")).decode("utf-8")
        except Exception:
            license_err = parts[2]

        try:
            email_from_token = base64.urlsafe_b64decode(parts[3].encode("ascii")).decode("utf-8")
        except Exception:
            email_from_token = parts[3]
    else:
        license_err = parts[2] if len(parts) > 2 else ""

    try:
        login_info = CallGetSubscriberLoginInfo(subscriber_code=subscriber_code)
        # Mostrar el email como "usuario alternativo" cuando viene en el token.
        login2_display = (email_from_token or "").strip() or (login_info.get("login2") or "")
        context = {
            "login2": login2_display,
            "login1": login_info.get("login1") or "",
            "password": login_info.get("password") or "",
            "license_block_added": True if str(license_ok) == "1" else False,
            "license_block_error": license_err or None,
        }
        return render(request, "wind/credentials.html", context)
    except Exception:
        return render(
            request,
            "wind/credentials.html",
            {"error": "No pudimos cargar tus credenciales en este momento. Intenta de nuevo en unos segundos."},
            status=500,
        )

