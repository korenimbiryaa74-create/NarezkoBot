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
