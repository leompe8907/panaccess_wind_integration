from django.urls import path
from wind.functions import (
    panaccess_session_status_view,
    singleton,
    logged_in_view,
    sync_subscribers_view,
    compare_and_update_subscribers_view,
    sync_products_view,
    test_call_list_products,
    products_stats_view,
    sync_smartcards_view,
    test_call_list_smartcards,
    smartcards_stats_view,
    create_subscriber_view,
    validate_subscriber_email_view,
    change_password_view,
    full_sync_view,
)
from wind.views import (
    login_test_view,
    login_page_view,
    dashboard_view,
    subscriber_test_view,
    login_facebook_test_view,
    register_view,
    credentials_view,
    forgot_password_view,
    reset_password_view,
    delete_account_info_view,
    RequestUDIDManualView,
    ValidateAndAssociateUDIDView,
    AuthenticateWithUDIDView,
    ValidateStatusUDIDView,
    DisassociateUDIDView,
)
from wind.auth_views import GoogleLoginView, FacebookLoginView

urlpatterns = [
    # Portal de usuario
    path('', login_page_view, name='home'),
    path('login/', login_page_view, name='login'),
    path('dashboard/', dashboard_view, name='dashboard'),
    path('subscriber-test/', subscriber_test_view, name='subscriber_test'),

    # Autenticación Social vía REST API (Token de Google a JWT Django)
    path('auth/google/', GoogleLoginView.as_view(), name='google_login_api'),
    
    # Autenticación Social vía REST API (Token de Facebook a JWT Django)
    path('auth/facebook/', FacebookLoginView.as_view(), name='facebook_login_api'),

    # Página de prueba: Iniciar sesión con Google (nativo HTML)
    path('login-test/', login_test_view, name='login_test'),
    
    # Página de prueba: Iniciar sesión con Facebook (SDK JS)
    path('login-test-facebook/', login_facebook_test_view, name='login_test_facebook'),
    
    # Registro web (formulario usable)
    path('register/', register_view, name='register_web'),
    # Página para mostrar credenciales recién creadas (token firmado)
    path('credentials/', credentials_view, name='credentials_web'),
    path('forgot-password/', forgot_password_view, name='forgot_password'),
    path('reset-password/', reset_password_view, name='reset_password'),
    path('eliminar-cuenta/', delete_account_info_view, name='delete_account_info'),
    
    # Operaciones PanAccess (staff)
    path('ops/panaccess-session/', panaccess_session_status_view, name='panaccess_session'),
    path('logged-in/', logged_in_view, name='logged_in'),
    path('singleton/', singleton, name='singleton'),
    
    # Sincronización de suscriptores
    path('sync-subscribers/', sync_subscribers_view, name='sync_subscribers'),
    path('compare-and-update-subscribers/', compare_and_update_subscribers_view, name='compare_and_update_subscribers'),
    
    # Endpoints de productos
    path('sync-products/', sync_products_view, name='sync_products'),
    path('test-call-list-products/', test_call_list_products, name='test_call_list_products'),
    path('products-stats/', products_stats_view, name='products_stats'),
    
    # Endpoints de smartcards
    path('sync-smartcards/', sync_smartcards_view, name='sync_smartcards'),
    path('test-call-list-smartcards/', test_call_list_smartcards, name='test_call_list_smartcards'),
    path('smartcards-stats/', smartcards_stats_view, name='smartcards_stats'),
    
    # Endpoint para crear suscriptores
    path('create-subscriber/', create_subscriber_view, name='create_subscriber'),

    # Validar si el email del suscriptor ya existe
    path('validate-subscriber-email/', validate_subscriber_email_view, name='validate_subscriber_email'),

    # Cambio de contraseña (PanAccess)
    path('change-password/', change_password_view, name='change_password'),

    # Sincronización global (todas las tablas)
    path('full-sync/', full_sync_view, name='full_sync'),

    # Smart TV UDID pairing paths (manual flow)
    path('request-udid-manual/', RequestUDIDManualView.as_view(), name='request-udid-manual'),
    path('validate-and-associate-udid/', ValidateAndAssociateUDIDView.as_view(), name='validate-and-associate-udid'),
    path('authenticate-with-udid/', AuthenticateWithUDIDView.as_view(), name='authenticate-with-udid'),
    path('validate/', ValidateStatusUDIDView.as_view(), name='validate_udid'),
    path('disassociate-udid/', DisassociateUDIDView.as_view(), name='disassociate-udid'),
]
