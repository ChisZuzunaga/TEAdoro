# Si no lo has hecho aún:
python -m venv .venv
.venv\Scripts\activate   # activa el entorno

# Instala dependencias:
pip install fastapi uvicorn pydub python-dotenv openai

# Ejecuta el servidor:
uvicorn app:app --host 0.0.0.0 --port 8000
