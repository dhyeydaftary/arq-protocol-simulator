"""Run the API with Flask's development server: python -m arq_api"""

from . import app

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
