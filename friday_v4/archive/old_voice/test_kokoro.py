import sys
import numpy as np
import soundfile as sf
from kokoro import KPipeline

pipeline = KPipeline(lang_code='a')
generator = pipeline("Hello, this is a test.", voice="af_bella", speed=1.0)
print(f"Generator created: {generator}")
for i, result in enumerate(generator):
    print(f"Yielded {i}: {type(result)}")
    print(dir(result))
    if isinstance(result, tuple):
        print(f"Tuple len: {len(result)}")
