import re
def normalize_key(value):
    return re.sub(r'[ _-]+','-',value).lower()
