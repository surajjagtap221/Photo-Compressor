import boto3
import io
import os
import json
import logging

from PIL import Image

logger = logging.getLogger()
logger.setLevel(logging.INFO)

RAW_BUCKET        = os.environ.get("RAW_BUCKET",        "photo-compresser-primary-bucket")
COMPRESSED_BUCKET = os.environ.get("COMPRESSED_BUCKET", "photo-compresser-secondary-bucket")
SNS_TOPIC_ARN     = os.environ.get("SNS_TOPIC_ARN",     "arn:aws:sns:us-east-1:067828305096:file-compressed")

JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "60"))
MAX_WIDTH    = int(os.environ.get("MAX_WIDTH",    "1920"))
MAX_HEIGHT   = int(os.environ.get("MAX_HEIGHT",   "1080"))

s3  = boto3.client("s3")
sns = boto3.client("sns")


def lambda_handler(event, context):
    results = []
    for record in event["Records"]:
        result = process_record(record)
        results.append(result)
    return {
        "statusCode": 200,
        "body": json.dumps(results)
    }


def process_record(record):
    
    bucket          = record["s3"]["bucket"]["name"]
    key             = record["s3"]["object"]["key"]
    orig_size_bytes = record["s3"]["object"].get("size", 0)

    response    = s3.get_object(Bucket=bucket, Key=key)
    image_bytes = response["Body"].read()

    compressed_bytes, out_format = compress_image(image_bytes, key)
    compressed_size = len(compressed_bytes)

    savings_pct = round((1 - compressed_size / orig_size_bytes) * 100, 1) if orig_size_bytes else 0

    output_key = build_output_key(key, out_format)
    s3.put_object(
        Bucket=COMPRESSED_BUCKET,
        Key=output_key,
        Body=compressed_bytes,
        ContentType=f"image/{out_format.lower()}",
        CacheControl="max-age=86400",
    )

    notify_sns(
        original_key=key,
        output_key=output_key,
        original_size=orig_size_bytes,
        compressed_size=compressed_size,
        savings_pct=savings_pct,
        compressed_bucket=COMPRESSED_BUCKET
    )

    return {
        "original":              f"s3://{bucket}/{key}",
        "compressed":            f"s3://{COMPRESSED_BUCKET}/{output_key}",
        "original_size_bytes":   orig_size_bytes,
        "compressed_size_bytes": compressed_size,
        "savings_percent":       savings_pct
    }


def compress_image(image_bytes: bytes, original_key: str):
    img = Image.open(io.BytesIO(image_bytes))

    if img.mode in ("P", "CMYK"):
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")

    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)

    ext = original_key.rsplit(".", 1)[-1].lower()
    if ext in ("jpg", "jpeg"):
        out_format = "JPEG"
    elif ext == "png":
        out_format = "PNG" if has_alpha else "JPEG"
    else:
        out_format = "PNG" if has_alpha else "JPEG"

    img.thumbnail((MAX_WIDTH, MAX_HEIGHT), Image.LANCZOS)

    buffer = io.BytesIO()
    if out_format == "JPEG":
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    else:
        img.save(buffer, format="PNG", optimize=True, compress_level=7)

    buffer.seek(0)
    return buffer.read(), out_format


def build_output_key(original_key: str, out_format: str) -> str:
    name, _ = original_key.rsplit(".", 1) if "." in original_key else (original_key, "")
    extension = "jpg" if out_format == "JPEG" else "png"
    return f"{name}_compressed.{extension}"


def notify_sns(original_key, output_key, original_size, compressed_size, savings_pct, compressed_bucket):
    message_body = {
        "status":             "SUCCESS",
        "original_file":      original_key,
        "compressed_file":    output_key,
        "compressed_bucket":  compressed_bucket,
        "original_size_kb":   round(original_size   / 1024, 2),
        "compressed_size_kb": round(compressed_size / 1024, 2),
        "savings_percent":    savings_pct,
        "access_url":         f"https://{compressed_bucket}.s3.amazonaws.com/{output_key}"
    }

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=f"Photo compressed - {savings_pct}% smaller",
        Message=json.dumps(message_body, indent=2)
    )