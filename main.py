import asyncio
import os
import time
import httpx
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query, Request, Body
from typing import Optional, List
from pydantic import BaseModel, Field
from typing import Optional, List, Union
from PIL import Image
import io
from dotenv import load_dotenv

load_dotenv()

SERVICE_TITLE = os.getenv("SERVICE_TITLE", "RunningHub Integration Service")
app = FastAPI(title=SERVICE_TITLE)

RH_BASE_URL = "https://www.runninghub.cn"
RH_API_KEY = os.getenv("RUNNINGHUB_API_KEY")
RH_WEBAPP_ID = os.getenv("RUNNINGHUB_WEBAPP_ID", "2028478697426128898")
RH_INPUT_NODE_ID = os.getenv("RUNNINGHUB_INPUT_NODE_ID", "10")
RH_FIELD_NAME = os.getenv("RUNNINGHUB_FIELD_NAME", "image")

async def get_closest_aspect_ratio(image_url: str) -> str:
    """Download image header and determine the closest RunningHub aspect ratio."""
    valid_ratios = {
        "1:1": 1.0,
        "16:9": 16/9,
        "9:16": 9/16,
        "4:3": 4/3,
        "3:4": 3/4,
        "3:2": 3/2,
        "2:3": 2/3,
        "5:4": 5/4,
        "4:5": 4/5,
        "21:9": 21/9
    }
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            # Try to get only the beginning of the file to save bandwidth
            headers = {"Range": "bytes=0-10240"}
            resp = await client.get(image_url, headers=headers)
            if resp.status_code not in [200, 206]:
                resp = await client.get(image_url) # Fallback to full download
            
            img = Image.open(io.BytesIO(resp.content))
            width, height = img.size
            actual_ratio = width / height
            
            # Find the ratio with the smallest difference
            closest_label = "1:1"
            min_diff = float('inf')
            for label, value in valid_ratios.items():
                diff = abs(actual_ratio - value)
                if diff < min_diff:
                    min_diff = diff
                    closest_label = label
            
            print(f"DEBUG - Auto Detect: {width}x{height} (Ratio: {actual_ratio:.3f}) -> Target: {closest_label}")
            return closest_label
    except Exception as e:
        print(f"ERROR - Aspect ratio detection failed: {e}")
        return "1:1" # Safe default

class ExecuteRequest(BaseModel):
    image_url: Optional[str] = None
    apiKey: Optional[str] = None
    webappId: Optional[str] = None
    input_node_id: Optional[str] = None
    field_name: Optional[str] = None

@app.get("/")
async def root():
    return {"message": "RunningHub Service is running. Use /v1/health or /v1/execute"}

@app.get("/v1/health")
async def health():
    return {"status": "ok", "service": "runninghub-integration"}

@app.get("/v1/debug-config")
async def debug_config():
    # Mask the API key for security, only show last 4 chars
    masked_key = f"****{RH_API_KEY[-4:]}" if RH_API_KEY else "NOT_SET"
    return {
        "RH_BASE_URL": RH_BASE_URL,
        "RH_API_KEY_MASKED": masked_key,
        "RH_WEBAPP_ID": RH_WEBAPP_ID,
        "RH_INPUT_NODE_ID": RH_INPUT_NODE_ID,
        "RH_FIELD_NAME": RH_FIELD_NAME,
        "ENV_VAR_SET": "RUNNINGHUB_API_KEY" in os.environ
    }

async def upload_to_rh(file_content: bytes, filename: str, api_key: str):
    max_attempts = 3
    timeout = 60.0
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                files = {"file": (filename, file_content)}
                data = {"apiKey": api_key, "fileType": "image"}
                print(f"Uploading to RH (Attempt {attempt+1}/{max_attempts})...")
                response = await client.post(f"{RH_BASE_URL}/task/openapi/upload", data=data, files=files)
                res_json = response.json()
                if res_json.get("code") != 0:
                    print(f"RH Upload Warning: {res_json}")
                    if attempt == max_attempts - 1:
                        raise HTTPException(status_code=502, detail=f"RH Upload Failed after {max_attempts} attempts: {res_json}")
                    await asyncio.sleep(2)
                    continue
                return res_json["data"]["fileName"]
        except Exception as e:
            print(f"RH Upload Exception (Attempt {attempt+1}/{max_attempts}): {str(e)}")
            if attempt == max_attempts - 1:
                raise HTTPException(status_code=502, detail=f"RH Upload Error after {max_attempts} attempts: {str(e)}")
            await asyncio.sleep(2)

@app.post("/v1/execute")
async def execute(
    request: Request,
    image_file: Optional[UploadFile] = File(None)
):
    print(f"Incoming Request: {request.method} {request.url}")
    
    # 1. Start with defaults
    target_image_url = None
    target_api_key = RH_API_KEY
    target_webapp_id = RH_WEBAPP_ID
    target_node_id = RH_INPUT_NODE_ID
    target_field = RH_FIELD_NAME

    # 2. Try to get data from JSON body
    try:
        json_data = await request.json()
        print(f"Manual JSON Parse: {json_data}")
        if json_data:
            target_image_url = json_data.get("image_url") or json_data.get("imageUrl")
            target_api_key = json_data.get("apiKey") or json_data.get("api_key") or target_api_key
            target_webapp_id = json_data.get("webappId") or json_data.get("webapp_id") or target_webapp_id
            target_node_id = json_data.get("input_node_id") or json_data.get("nodeId") or target_node_id
            target_field = json_data.get("field_name") or json_data.get("fieldName") or target_field
    except Exception:
        # Not a JSON request or empty
        pass

    # 3. Try to get data from Form if still missing image_url
    if not target_image_url and not image_file:
        try:
            form_data = await request.form()
            print(f"Manual Form Parse: {list(form_data.keys())}")
            target_image_url = form_data.get("image_url") or form_data.get("imageUrl")
            target_api_key = form_data.get("apiKey") or form_data.get("api_key") or target_api_key
            target_webapp_id = form_data.get("webappId") or form_data.get("webapp_id") or target_webapp_id
            target_node_id = form_data.get("input_node_id") or form_data.get("nodeId") or target_node_id
            target_field = form_data.get("field_name") or form_data.get("fieldName") or target_field
        except Exception:
            pass

    # 4. Final fallback: Query Parameters
    if not target_image_url and not image_file:
        target_image_url = request.query_params.get("image_url")

    # Validation
    if not target_image_url and not image_file:
        # Log what we have for debugging
        print(f"FAIL: No image found. Params: key={target_api_key[:5]}... webapp={target_webapp_id}")
        raise HTTPException(status_code=400, detail="No image provided. Please send 'image_url' in JSON or Form body.")

    print(f"Resolved Params: img={target_image_url or 'FILE'}, node={target_node_id}, field={target_field}")

    target_image_name = ""

    # 1. Handle Input (Upload or URL)
    if image_file:
        content = await image_file.read()
        target_image_name = await upload_to_rh(content, image_file.filename, target_api_key)
    elif target_image_url:
        max_attempts = 3
        timeout = 60.0
        success = False
        last_error = ""

        for attempt in range(max_attempts):
            print(f"Downloading image from: {target_image_url} (Attempt {attempt+1}/{max_attempts})")
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
                async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
                    resp = await client.get(target_image_url)
                    print(f"Download Response Status: {resp.status_code}")
                    if resp.status_code == 200:
                        print(f"Image downloaded successfully. Size: {len(resp.content)} bytes")
                        target_image_name = await upload_to_rh(resp.content, "input_image.jpg", target_api_key)
                        success = True
                        break
                    else:
                        last_error = f"Status: {resp.status_code}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)}"
                print(f"Download Attempt {attempt+1} failed: {last_error}")
            
            if attempt < max_attempts - 1:
                await asyncio.sleep(2)
        
        if not success:
            raise HTTPException(status_code=400, detail=f"Failed to download image after {max_attempts} attempts. Last error: {last_error}")
    else:
        raise HTTPException(status_code=400, detail="No image provided")

    # 2. Run the App
    node_payload = [
        {
            "nodeId": str(target_node_id),
            "fieldName": target_field,
            "fieldValue": target_image_name
        }
    ]

    async with httpx.AsyncClient() as client:
        run_payload = {
            "apiKey": target_api_key,
            "webappId": int(target_webapp_id),
            "nodeInfoList": node_payload
        }
        print(f"Sending Run Payload to RH: {run_payload}")
        run_resp = await client.post(f"{RH_BASE_URL}/task/openapi/ai-app/run", json=run_payload)
        run_json = run_resp.json()
        
        if run_json.get("code") != 0:
            raise HTTPException(status_code=502, detail=f"RH Run Failed: {run_json}")
        
        task_id = run_json["data"]["taskId"]

    # 3. Polling for results
    max_retries = int(os.getenv("MAX_RETRIES", "60"))
    async with httpx.AsyncClient() as client:
        for _ in range(max_retries):
            poll_resp = await client.post(
                f"{RH_BASE_URL}/task/openapi/outputs", 
                json={"taskId": task_id, "apiKey": target_api_key}
            )
            poll_json = poll_resp.json()
            
            code = poll_json.get("code")
            if code == 0:
                # Success! Return all outputs
                return {
                    "status": "success",
                    "taskId": task_id,
                    "outputs": poll_json["data"] # This is usually an array of URLs
                }
            elif code in (804, 813):
                # 804 = Still running, 813 = Queued
                await asyncio.sleep(5)
                continue
            else:
                raise HTTPException(status_code=502, detail=f"RH Task Failed: {poll_json}")
        
    raise HTTPException(status_code=504, detail="Timeout waiting for RunningHub task")

from typing import Optional, List, Union

class NanoBananaRequest(BaseModel):
    image_url: Optional[str] = None
    original_image_url: Optional[str] = None
    reference_image_url: Optional[str] = None
    reference_image_urls: Optional[Union[str, List[str]]] = None
    prompt: str
    resolution: Optional[str] = Field(default="4k", description="Resolution: 1k, 2k, or 4k")
    aspect_ratio: Optional[str] = Field(default=None, description="Aspect Ratio: 1:1, 16:9, 9:16, etc.")
    aspectRatio: Optional[str] = None # Alias
    apiKey: Optional[str] = None
    api_key: Optional[str] = None # Alias

@app.post("/v1/execute-nanobanana")
async def execute_nanobanana(req: NanoBananaRequest):
    api_key = req.apiKey or req.api_key or RH_API_KEY
    if not api_key:
        raise HTTPException(status_code=500, detail="RunningHub API Key not configured")

    # 1. Prepare image URLs array
    main_image = req.original_image_url or req.image_url
    if not main_image:
        raise HTTPException(status_code=422, detail="Missing original_image_url or image_url")
        
    image_urls = [main_image]
    
    # Handle references (both singular and plural, string or list)
    refs = []
    if req.reference_image_url:
        refs.append(req.reference_image_url)
    
    if req.reference_image_urls:
        if isinstance(req.reference_image_urls, str):
            if req.reference_image_urls not in refs:
                refs.append(req.reference_image_urls)
        elif isinstance(req.reference_image_urls, list):
            for r in req.reference_image_urls:
                if r and r not in refs:
                    refs.append(r)
    
    image_urls.extend(refs)

    # Max 10 images supported by API
    if len(image_urls) > 10:
        image_urls = image_urls[:10]

    payload = {
        "imageUrls": image_urls,
        "prompt": req.prompt,
        "resolution": req.resolution
    }
    
    # Valid ratios for RunningHub N-Pro
    valid_ratios = ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9"]
    
    raw_aspect = req.aspect_ratio or req.aspectRatio
    detected_aspect = "9:16" # Safe default

    if not raw_aspect or str(raw_aspect).lower() in ["auto", "match_input_image", "original", "none"]:
        print(f"DEBUG - Attempting auto-detection of aspect ratio for {main_image}")
        detected_aspect = await get_closest_aspect_ratio(main_image)
    else:
        # Normalize and check provided ratio
        clean_aspect = str(raw_aspect).replace("_", ":").replace(" ", ":").strip()
        if clean_aspect in valid_ratios:
            detected_aspect = clean_aspect
        else:
            print(f"DEBUG - Invalid aspect '{raw_aspect}', defaulting to auto-detect")
            detected_aspect = await get_closest_aspect_ratio(main_image)

    payload["aspectRatio"] = detected_aspect

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    print(f"Starting Nano Banana Pro task with {len(image_urls)} images...")
    
    # 2. Start Task
    async with httpx.AsyncClient(timeout=60.0) as client:
        url = "https://www.runninghub.ai/openapi/v2/rhart-image-n-pro/edit"
        try:
            resp = await client.post(url, headers=headers, json=payload)
            res_data = resp.json()
        except httpx.RequestError as exc:
            print(f"An error occurred while requesting {exc.request.url!r}.")
            raise HTTPException(status_code=504, detail=f"Connection Error to RunningHub API: {str(exc)}")

        if resp.status_code != 200 or not res_data.get("taskId"):
            raise HTTPException(status_code=502, detail=f"Failed to start Nano Banana task: {res_data}")

        task_id = res_data["taskId"]
        print(f"Task {task_id} queued. Polling for results...")

        # 3. Polling for Success
        query_url = "https://www.runninghub.ai/openapi/v2/query"
        max_retries = 60 # 5 minutes total
        
        for _ in range(max_retries):
            await asyncio.sleep(5)
            try:
                poll_resp = await client.post(query_url, headers=headers, json={"taskId": task_id})
                poll_data = poll_resp.json()
            except httpx.RequestError as exc:
                print(f"Timeout during polling: {exc}")
                continue # Retry on momentary connection drops
                
            status = poll_data.get("status")
            if status == "SUCCESS":
                images = poll_data.get("results", [])
                if images:
                    return {
                        "status": "success",
                        "taskId": task_id,
                        "output_url": images[0].get("url")
                    }
                else:
                    raise HTTPException(status_code=500, detail="Task succeeded but no images returned")
            
            elif status in ["FAILED", "EXCEPTION", "CANCELLED"]:
                error_msg = poll_data.get("errorMessage", "Unknown error")
                print(f"Task Failed: {error_msg}")
                raise HTTPException(status_code=502, detail=f"Image generation failed: {error_msg}")
            
            # If status changes to RUNNING or QUEUED, loop continues
        
        raise HTTPException(status_code=504, detail="Timeout waiting for Nano Banana task to complete")
