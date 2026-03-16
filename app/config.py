from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    groq_api_key: str = ""
    resend_api_key: str = ""
    allowed_emails: str = "*@scao.it"
    admin_emails: str = ""
    db_path: str = "searchdata/search.db"
    app_db_path: str = "data/app.db"
    secret_key: str = "change-me"
    default_monthly_token_limit: int = 500_000
    default_max_messages_per_conversation: int = 20
    production: bool = False          # True in cloud: enables Secure cookies, trusts proxy headers
    base_url: str = ""                 # Public URL (e.g., https://os1docs.ai.scao.it) for CORS/redirects
    docs_repo_path: str = "../os1-documentation/Claude Code Playground"
    groq_input_price: float = 0.05   # $ per million tokens (llama-3.1-8b-instant)
    groq_output_price: float = 0.08  # $ per million tokens (llama-3.1-8b-instant)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
