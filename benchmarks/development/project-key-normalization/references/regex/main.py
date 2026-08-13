import re

def normalize_key(value):
    if not value.isascii() or re.search(r'[^A-Za-z0-9 _-]', value):
        raise ValueError('invalid key')
    result = re.sub(r'[ _-]+', '-', value.strip()).strip('-').lower()
    if not result:
        raise ValueError('empty key')
    return result
