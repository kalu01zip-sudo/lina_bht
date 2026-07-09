import os
import asyncio
import httpx
import logging
from typing import Dict, Any, Optional, List
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

class YouCamClientError(Exception):
    pass

class YouCamClient:
    def __init__(self):
        self.api_key = os.getenv("YOUCAM_API_KEY")
        if not self.api_key:
            raise ValueError("YOUCAM_API_KEY environment variable is not set")
            
        self.base_url = "https://yce-api-01.makeupar.com/s2s/v2.0"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    async def _init_file_upload(self, file_name: str, file_size: int, content_type: str = "image/png") -> Dict[str, Any]:
        """Step 1: Get presigned URL and file ID from YouCam File API."""
        url = f"{self.base_url}/file/skin-analysis"
        payload = {
            "files": [
                {
                    "content_type": content_type,
                    "file_name": file_name,
                    "file_size": file_size
                }
            ]
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self.headers, json=payload, timeout=30.0)
            
            if response.status_code != 200:
                raise YouCamClientError(f"File API initialization failed: {response.text}")
                
            data = response.json()
            if data.get("status") != 200:
                raise YouCamClientError(f"File API error: {data}")
                
            file_data = data["data"]["files"][0]
            return {
                "file_id": file_data["file_id"],
                "upload_request": file_data["requests"][0]
            }

    async def _upload_to_s3(self, upload_request: Dict[str, Any], image_bytes: bytes):
        """Step 2: Upload actual image bytes to the provided S3 presigned URL."""
        url = upload_request["url"]
        method = upload_request["method"]
        headers = upload_request["headers"]
        
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=image_bytes,
                timeout=60.0
            )
            
            if response.status_code not in (200, 201, 204):
                raise YouCamClientError(f"Failed to upload image to S3: {response.text}")

    async def _create_ai_task(self, file_id: str) -> str:
        """Step 3: Create the AI Task for Skin Analysis with HD mode configurations."""
        url = f"{self.base_url}/task/skin-analysis"
        
        # As per the requirements, we need these HD actions and mask overlays enabled.
        dst_actions = [
            "hd_acne", "hd_dark_circle", "hd_droopy_lower_eyelid", "hd_droopy_upper_eyelid", 
            "hd_eye_bag", "hd_firmness", "hd_moisture", "hd_oiliness", "hd_pore", 
            "hd_radiance", "hd_redness", "hd_age_spot", "hd_texture", "hd_wrinkle", 
            "hd_skin_type", "hd_tear_trough"
        ]
        
        payload = {
            "src_file_id": file_id,
            "dst_actions": dst_actions,
            "miniserver_args": {
                "enable_mask_overlay": True
            },
            "format": "json"
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(url, headers=self.headers, json=payload, timeout=30.0)
            
            if response.status_code != 200:
                raise YouCamClientError(f"Failed to create AI task: {response.text}")
                
            data = response.json()
            if data.get("status") != 200:
                raise YouCamClientError(f"Task API error: {data}")
                
            return data["data"]["task_id"]

    async def _poll_task_status(self, task_id: str, max_retries: int = 60, delay: int = 3) -> Dict[str, Any]:
        """Step 4: Poll the task status until completion or failure."""
        url = f"{self.base_url}/task/skin-analysis/{task_id}"
        
        async with httpx.AsyncClient() as client:
            for _ in range(max_retries):
                response = await client.get(url, headers=self.headers, timeout=30.0)
                
                if response.status_code != 200:
                    raise YouCamClientError(f"Failed to poll task status: {response.text}")
                    
                data = response.json()
                if data.get("status") != 200:
                    raise YouCamClientError(f"Poll API error: {data}")
                    
                task_status = data["data"].get("task_status")
                
                if task_status == "success":
                    return data["data"]
                elif task_status == "error":
                    raise YouCamClientError(f"YouCam engine failed to process the task: {data}")
                    
                # If running/pending, wait and retry
                await asyncio.sleep(delay)
                
            raise YouCamClientError(f"Task polling timed out after {max_retries * delay} seconds.")

    async def analyze_skin(self, image_bytes: bytes, file_name: str = "upload.png", content_type: str = "image/png") -> Dict[str, Any]:
        """
        Complete flow: 
        1. Init File Upload
        2. Upload to S3
        3. Create Task
        4. Poll Status
        5. Return Results
        """
        try:
            # 1. Init Upload
            file_size = len(image_bytes)
            init_data = await self._init_file_upload(file_name, file_size, content_type)
            
            # 2. Upload Image
            await self._upload_to_s3(init_data["upload_request"], image_bytes)
            
            # 3. Create Task
            task_id = await self._create_ai_task(init_data["file_id"])
            
            # 4. Poll and Return
            results = await self._poll_task_status(task_id)
            return results
        except Exception as e:
            logger.error(f"Error in YouCam skin analysis: {str(e)}")
            raise

youcam_client = YouCamClient()
