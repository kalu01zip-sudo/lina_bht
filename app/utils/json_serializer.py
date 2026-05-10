from datetime import datetime
from bson import ObjectId


def serialize_mongo(data):

    if isinstance(data, list):

        return [
            serialize_mongo(item)
            for item in data
        ]

    if isinstance(data, dict):

        result = {}

        for key, value in data.items():

            result[key] = serialize_mongo(
                value
            )

        return result

    if isinstance(data, datetime):

        return data.isoformat()

    if isinstance(data, ObjectId):

        return str(data)

    return data