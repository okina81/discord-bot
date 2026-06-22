import os
import datetime
from dotenv import load_dotenv
from google import genai as google_genai

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
APEX_API_KEY = os.getenv("APEX_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = google_genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

JST = datetime.timezone(datetime.timedelta(hours=9))

TARGET_USER_IDS = [512510702129512469, 1133749381250695269]
ALLOWED_CHANNEL_IDS = [1509231531326181406]
RECRUIT_KEYWORDS = ["募集", "募", "ぼ"]
RECRUIT_CHANNEL_ID = 1134854694645276703
