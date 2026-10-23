from dotenv import load_dotenv

load_dotenv()

import os
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.sessions import SessionMiddleware


from routes.crud import crud
from routes.payment import polar
from routes.report import report
from routes.analysis import analysis
from routes.agent import agent_routes
from services.utils import get_logger
from routes.chatbot import chat_routes
from services.rate_limiter import limiter
from routes.auth import google_sso, local_auth

logger = get_logger(__name__)
app = FastAPI()

environment = os.environ.get("ENVIRONMENT")
if environment == "development":
    origins = [
        "https://www.smartstructs.org",
        "https://app.smartstructs.org",
        "http://localhost",  # Standard HTTP localhost
        "http://localhost:3000",  # Common React/Next.js port
        "http://localhost:5173",  # Common Vite port
        "http://127.0.0.1",  # Loopback IP
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]
else:
    origins = [
        "https://www.smartstructs.org",
        "https://app.smartstructs.org",
    ]

app.add_middleware(SessionMiddleware, secret_key=os.environ["APP_SECRET"])
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # any origin
    allow_credentials=True,
    allow_methods=["*"],  # any HTTP method
    allow_headers=["*"],
    # any headers
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

app.include_router(agent_routes.router, prefix="", tags=["Agent"])
app.include_router(chat_routes.router, prefix="", tags=["Chatbot"])
app.include_router(local_auth.router, prefix="", tags=["Auth"])
app.include_router(google_sso.router, prefix="", tags=["Auth"])
app.include_router(crud.router, prefix="", tags=["CRUD"])
app.include_router(analysis.router, prefix="", tags=["Analysis"])
app.include_router(polar.router, prefix="", tags=["Payment Gateway"])
app.include_router(report.router, prefix="", tags=["Report"])


@app.get("/health")
async def health(request: Request):
    try:
        return JSONResponse(content="App running...", status_code=200)
    except Exception as e:
        logger.critical("Error: ", e)
        raise HTTPException(status_code=500, detail="Something went wrong")
