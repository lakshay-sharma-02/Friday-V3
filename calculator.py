def process_numbers(a, b, op):
    if op == 'div' and b == 0:
        raise ValueError("Zero division")
    ops = {
        'add': a + b,
        'sub': a - b,
        'mul': a * b,
        'div': a / b,
    }
    if op not in ops:
        raise ValueError("Bad op")
    return ops[op]
