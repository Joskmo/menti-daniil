def next_id(rows):
    if not rows:
        return 1
    highest = rows[0]['id']
    for row in rows[1:]:
        if row['id'] > highest:
            highest = row['id']
    return highest + 1
