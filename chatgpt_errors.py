"""Typed browser errors shared without loading browser dependencies."""

class ChatGPTWebError(RuntimeError):
    pass

class ChatGPTSafetyError(ChatGPTWebError):
    safety_blocked = True

class ChatGPTLoginError(ChatGPTWebError):
    pass

class ChatGPTQuotaError(ChatGPTWebError):
    quota_blocked = True

class ChatGPTTechnicalError(ChatGPTWebError):
    pass

class ChatGPTUploadFailed(ChatGPTWebError):
    """ChatGPT showed «Загрузка не удалась» (network): the file was not accepted and nothing was sent,
    so the page can be reloaded and the request repeated safely."""
    pass
