"""Explicit, channel-local edits. No model calls, queue work or index changes."""
from ..security.secrets import (
    SecretDecryptError, SecretStoreError, decrypt_secret, encrypt_secret,
)


class ConnectionUpdateError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def apply_embedding_connection(row, values: dict) -> None:
    """Apply a validated embedding patch, copying saved chat credentials only on opt-in.

    No secret is returned; model selection and the active index remain independent.
    Validate all inputs before mutating the row, including the optional source cipher.
    """
    provider = values["provider"]
    action = values.get("api_key_action", "keep")
    key = values.get("api_key")
    url = values.get("base_url")
    cipher = None
    reuse = values.get("reuse_chat", False)
    if reuse:
        if provider != "openai" or action != "keep" or key:
            raise ConnectionUpdateError("reuse_conflict", "复制已保存连接时请选择 OpenAI 兼容，并勿同时替换或清除密钥")
        if row.provider != "openai" or not row.openai_base_url or not row.has_api_key or not row.openai_api_key_cipher:
            raise ConnectionUpdateError("reuse_requires_chat", "请先保存 OpenAI 兼容对话连接的地址和密钥")
        try:
            saved_key = decrypt_secret(row.openai_api_key_cipher)
            if not saved_key:
                raise ConnectionUpdateError("reuse_requires_chat", "已保存的对话密钥为空，请重新保存")
            cipher = encrypt_secret(saved_key)
        except (SecretStoreError, SecretDecryptError):
            raise ConnectionUpdateError("reuse_key_unavailable", "已保存的对话密钥无法读取，请重新保存", 500) from None
        url = row.openai_base_url
    else:
        if provider != "disabled" and not url:
            raise ConnectionUpdateError("embedding_url_required", "请填写向量服务地址")
        if action == "replace":
            if provider != "openai" or not key or not key.strip():
                raise ConnectionUpdateError("embedding_key_required", "请选择 OpenAI 兼容并填写要保存的密钥")
            cipher = encrypt_secret(key.strip())
        # Never silently send an existing secret to a new destination.
        if provider == "openai" and action == "keep" and row.has_embedding_api_key and (
            row.embedding_provider != "openai" or
            (url or "").rstrip("/") != (row.embedding_base_url or "").rstrip("/")
        ):
            raise ConnectionUpdateError("embedding_key_reconfirm", "服务地址已改变，请重新填写密钥或明确复制已保存的对话连接")

    row.embedding_provider = provider
    if url:
        row.embedding_base_url = url
    if "model" in values:
        row.embedding_model = values["model"] or None
    if values.get("timeout_seconds") is not None:
        row.embedding_timeout_seconds = values["timeout_seconds"]
    if cipher is not None:
        row.embedding_api_key_cipher = cipher
        row.has_embedding_api_key = True
    elif action == "clear":
        row.embedding_api_key_cipher = None
        row.has_embedding_api_key = False
