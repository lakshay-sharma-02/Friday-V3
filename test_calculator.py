import pytest
from calculator import process_numbers

def test_add():
    assert process_numbers(1, 2, 'add') == 3

def test_sub():
    assert process_numbers(5, 3, 'sub') == 2

def test_mul():
    assert process_numbers(2, 4, 'mul') == 8

def test_div():
    assert process_numbers(10, 2, 'div') == 5
    with pytest.raises(ValueError):
        process_numbers(10, 0, 'div')

def test_bad():
    with pytest.raises(ValueError):
        process_numbers(1, 1, 'bad')
