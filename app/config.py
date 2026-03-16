from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    groq_api_key: str = ""
    resend_api_key: str = ""
    allowed_emails: str = "*@scao.it"
    db_path: str = "data/search.db"
    secret_key: str = "change-me"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
