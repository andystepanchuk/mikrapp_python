import config


class AuthService:
    @staticmethod
    def verify(username: str, password: str) -> bool:
        return username == config.AUTH_USER and password == config.AUTH_PASS
