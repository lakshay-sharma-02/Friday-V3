import numpy as np
import plotly.graph_objects as go
from scipy.constants import pi

def generate_graph(func_name):
    z = np.linspace(-2 * pi, 2 * pi, 100)
    x = np.linspace(-2 * pi, 2 * pi, 100)
    X, Z = np.meshgrid(x, z)
    
    funcs = {
        "sin": np.sin(Z),
        "cos": np.cos(Z),
        "tan": np.tan(Z),
        "sec": 1/np.cos(Z),
        "csc": 1/np.sin(Z),
        "cot": 1/np.tan(Z)
    }
    
    if func_name not in funcs:
        raise ValueError("Invalid function")
        
    Y = funcs[func_name]
    # Handle discontinuities for tan/sec/csc/cot
    Y[np.abs(Y) > 10] = np.nan 

    fig = go.Figure(data=[go.Surface(z=Y, x=X, y=Z)])
    fig.update_layout(title=f'3D {func_name}', scene=dict(xaxis_title='X', yaxis_title='Z', zaxis_title=func_name))
    fig.show()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        generate_graph(sys.argv[1])
    else:
        for f in ["sin", "cos", "tan"]:
            generate_graph(f)

# ponytail: hardcoded functions, add dynamic lambda parser when complex expressions needed.