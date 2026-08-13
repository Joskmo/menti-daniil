def next_id(rows):
    return min((row['id'] for row in rows), default=0) + 1
