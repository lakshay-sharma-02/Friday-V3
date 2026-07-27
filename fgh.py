import sys

def calculate(expr):
    try:
        return eval(expr, {"__builtins__": None}, {})
    except Exception as e:
        return f"Error: {e}"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(calculate(" ".join(sys.argv[1:])))
    else:
        print("Usage: python fgh.py <expression>")

# ponytail: eval is unsafe for untrusted input; replace with ast.literal_eval or a parser if user input is required.