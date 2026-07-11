import os
from dotenv import load_dotenv
load_dotenv()

import uvicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    debug = os.environ.get("ENV", "development") != "production"
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, reload=debug)
