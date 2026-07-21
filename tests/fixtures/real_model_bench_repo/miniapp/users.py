def normalize_display_name(value):
    normalized = " ".join(str(value).split())
    return normalized or "anonymous"


def unique_usernames(values):
    raise NotImplementedError
