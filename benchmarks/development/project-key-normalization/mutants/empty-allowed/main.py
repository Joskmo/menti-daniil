import re
def normalize_key(value):
    if not value.isascii(): raise ValueError()
    return re.sub(r'[ _-]+','-',value.strip()).strip('-').lower()
