import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    print("❌ OPENAI_API_KEY was not found.")
    raise SystemExit(1)

print("API key was found in .env")

client = OpenAI(api_key=api_key)

model = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")

print(f"Testing model: {model}")

try:
    response = client.responses.create(
        model=model,
        input="Reply with exactly: ESTIMATE AI connection successful."
    )

    print("\nOPENAI CONNECTION SUCCESSFUL")
    print("AI response:")
    print(response.output_text)

except Exception as e:
    print("\nOPENAI CONNECTION FAILED")
    print(type(e).__name__)
    print(str(e))