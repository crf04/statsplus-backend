from app import create_app
from app.config.settings import get_runtime_settings

app = create_app()

if __name__ == '__main__':
    settings = get_runtime_settings()
    app.run(host='0.0.0.0', port=settings.port, debug=settings.debug)
