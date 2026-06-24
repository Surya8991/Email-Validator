from mangum import Mangum

from app.main import app  # noqa: F401

# Vercel serverless entry point — wraps the FastAPI ASGI app
handler = Mangum(app, lifespan="auto")
