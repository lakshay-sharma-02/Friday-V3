import sys
from friday_v4.voice.tts import KokoroProvider, XTTSProvider, EdgeTTSProvider, PyTTSProvider

print("Loading Kokoro...")
try:
    kp = KokoroProvider()
    print("Kokoro loaded!")
except Exception as e:
    print(f"Kokoro failed: {e}")

print("Loading XTTS...")
try:
    xtts = XTTSProvider()
    print("XTTS loaded!")
except Exception as e:
    print(f"XTTS failed: {e}")

print("Loading PyTTS...")
try:
    pt = PyTTSProvider()
    print("PyTTS loaded!")
except Exception as e:
    print(f"PyTTS failed: {e}")

