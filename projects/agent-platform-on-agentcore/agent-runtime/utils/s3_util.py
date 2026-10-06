import boto3
from urllib.parse import urlparse


def download_image_from_s3(s3_url: str) -> bytes:
    """S3 URL에서 이미지를 다운로드하여 bytes로 반환합니다.
    
    Args:
        s3_url: S3 이미지 URL (예: s3://bucket-name/path/to/image.jpg)
        
    Returns:
        이미지 데이터의 bytes
        
    Raises:
        Exception: S3 다운로드 실패 시
    """
    try:
        # S3 URL 파싱
        parsed_url = urlparse(s3_url)
        if parsed_url.scheme != 's3':
            raise ValueError(f"Invalid S3 URL: {s3_url}")
        
        bucket_name = parsed_url.netloc
        object_key = parsed_url.path.lstrip('/')
        
        # S3 클라이언트 생성
        s3_client = boto3.client('s3')
        
        # S3에서 객체 다운로드
        response = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        image_bytes = response['Body'].read()
        
        return image_bytes
        
    except Exception as e:
        raise Exception(f"Failed to download image from S3: {str(e)}")
