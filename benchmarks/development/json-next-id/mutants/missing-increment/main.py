def next_id(rows):
    return max((row['id'] for row in rows), default=1)
