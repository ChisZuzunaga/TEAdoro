# Si no lo has hecho aún:
python -m venv .venv
.venv\Scripts\activate   # activa el entorno

# Instala dependencias:
pip install fastapi uvicorn pydub python-dotenv openai

# Ejecuta el servidor:
uvicorn app:app --host 0.0.0.0 --port 8000




OPCIÓN B

pip install fastapi uvicorn pydub openai python-dotenv
pip install "git+https://github.com/openai/whisper.git"
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install gTTS
sudo apt-get install -y ffmpeg   # en Linux / WSL / EC2
