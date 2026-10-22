"""Explicit in-process unit-test hooks; production always uses PostgreSQL."""
import pytest


@pytest.fixture(autouse=True)
def isolated_worker_execution(monkeypatch):
    from apps.worker.core.config import settings
    from apps.api.services import model_admission as admission
    monkeypatch.setattr(settings, "task_execution_enabled", False)
    admission.configure_admission_backend(admission.InMemoryAdmissionBackend())
    admission.set_process_role("foreground")
    yield
    admission.reset_admission_backend()
    admission.set_process_role("foreground")
