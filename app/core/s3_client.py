import os
import boto3
import asyncio
from botocore.exceptions import ClientError

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_S3_BUCKET = os.getenv("AWS_S3_BUCKET") or os.getenv("AWS_S3_BUCKET_NAME")

# Initialize boto3 S3 client
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=AWS_REGION
)

def _upload_file_sync(file_bytes: bytes, file_path: str, content_type: str) -> str:
    """Synchronous function to upload file bytes to S3 and return the public URL."""
    if not AWS_S3_BUCKET:
        raise ValueError("[ERROR] AWS_S3_BUCKET not configured")
    
    # Clean leading slashes from file_path if any
    file_path = file_path.lstrip("/")

    s3_client.put_object(
        Bucket=AWS_S3_BUCKET,
        Key=file_path,
        Body=file_bytes,
        ContentType=content_type
    )
    
    # Construct the public S3 URL (assuming the bucket is public or public-read)
    url = f"https://{AWS_S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{file_path}"
    return url

async def upload_file_to_s3(file_bytes: bytes, file_path: str, content_type: str) -> str:
    """Asynchronous wrapper to upload file bytes to S3 using asyncio.to_thread."""
    return await asyncio.to_thread(_upload_file_sync, file_bytes, file_path, content_type)

def _delete_file_sync(file_path: str):
    """Synchronous function to delete a file from S3."""
    if not AWS_S3_BUCKET:
        raise ValueError("[ERROR] AWS_S3_BUCKET not configured")
    
    file_path = file_path.lstrip("/")
    try:
        s3_client.delete_object(Bucket=AWS_S3_BUCKET, Key=file_path)
    except ClientError as e:
        print(f"[WARN] Failed to delete file {file_path} from S3: {e}")

async def delete_file_from_s3(file_path: str):
    """Asynchronous wrapper to delete a file from S3 using asyncio.to_thread."""
    await asyncio.to_thread(_delete_file_sync, file_path)
