import httpx
import asyncio

ASCE_URL = "https://gis.asce.org/arcgis/rest/services/ASCE722/s2022_RiskCategory{rist_category}/ImageServer/identify"
map_rc = {'I':'1','II':'2','III':'3','IV':'4'}

from services.utils import get_logger

logger = get_logger(__name__)

async def get_snow_data(long: float, lat: float, risk_category: str):
    try:
        params = {
            # Notice the double {{ and }}
            "geometry": f'{{"x":{long},"y":{lat},"spatialReference":{{"wkid":4326}}}}',
            "geometryType": "esriGeometryPoint",
            "f": "json"
        }
        risk_category=map_rc[risk_category]

        # Use httpx.AsyncClient to make the non-blocking async request
        async with httpx.AsyncClient() as client:
            response = await client.get(ASCE_URL.format(rist_category=risk_category), params=params)
            response.raise_for_status() 
            response_json = response.json()

            if response_json["value"]=='NoData':
                return {'status': "error", 'detail':"Invalid long and lat. must be from USA"}

            return {"status":"success", "value":response_json["value"]}
    
    except Exception as e:
        logger.error(f"Error:{e}")
        return {'status': "error", 'detail':"Something Went Wrong during Snow data calc. Please try again later or ensure the lat/long are valid and based in USA"}