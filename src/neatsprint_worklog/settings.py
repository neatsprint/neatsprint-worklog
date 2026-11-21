from neatsprint_lib.settings import ServiceSettings


class Settings(ServiceSettings):
    service_name: str = "worklog"
    api_prefix: str = "work"


settings = Settings()
