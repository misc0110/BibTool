def check(entry, database):
    if len(entry["ID"]) == 0:
        return (False, "Citation key must be longer than 0 characters.") 

    return (True, None)
