import httpx
import asyncio

ASCE_URL = "https://gis.asce.org/arcgis/rest/services/ASCE722/w2022_mri700/ImageServer/identify"

from services.utils import get_logger

logger = get_logger(__name__)

async def get_wind_data(long: float, lat: float):
    try:
        params = {
            "geometry": f'{{"x":{long},"y":{lat},"spatialReference":{{"wkid":4326}}}}',
            "geometryType": "esriGeometryPoint",
            "f": "json"
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(ASCE_URL, params=params)
            response.raise_for_status() 
            response_json = response.json()

            if response_json["value"]=='NoData':
                return {'status': "error", 'detail':"Invalid long and lat. must be from USA"}

            return {"status":"success", "value":response_json["value"]}
            
    except Exception as e:
        logger.error(f"Error:{e}")
        return {'status': "error", 'detail':"Something Went Wrong during wind speed calc. Please try again later or ensure the lat/long are valid and based in USA"}

if __name__=='__main__':
    data = asyncio.run(get_wind_data(long=-104.99, lat=39.74))
    print(data)
            