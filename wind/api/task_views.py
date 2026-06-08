"""Estado de tareas Celery (staff)."""
from celery.result import AsyncResult
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status


@api_view(["GET"])
@permission_classes([IsAdminUser])
def task_status_view(request, task_id):
    """Consulta el estado de una tarea encolada (ej. full-sync)."""
    result = AsyncResult(task_id)
    payload = {
        "task_id": task_id,
        "state": result.state,
        "ready": result.ready(),
        "successful": result.successful() if result.ready() else None,
    }
    if result.ready():
        if result.successful():
            payload["result"] = result.result
        else:
            payload["error"] = str(result.result)
    return Response(payload, status=status.HTTP_200_OK)
