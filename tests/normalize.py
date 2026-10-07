"""Canonical instruction text shared by every side of a comparison."""
import re

def normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"\s*,\s*", ", ", text.strip().rstrip(";"))).strip()
