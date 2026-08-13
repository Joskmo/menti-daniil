def normalize_key(value):
    if not value.isascii():
        raise ValueError('invalid key')
    output = []
    pending_separator = False
    for character in value.strip():
        if character in ' _-':
            pending_separator = bool(output)
        elif character.isdigit() or 'a' <= character.lower() <= 'z':
            if pending_separator:
                output.append('-')
            output.append(character.lower())
            pending_separator = False
        else:
            raise ValueError('invalid key')
    result = ''.join(output).rstrip('-')
    if not result:
        raise ValueError('empty key')
    return result
