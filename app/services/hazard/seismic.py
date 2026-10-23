import asyncio
import httpx

USGC_URL =  'https://earthquake.usgs.gov/ws/building-codes/asce7-22/calculate'

from services.utils import get_logger

logger = get_logger(__name__)

async def get_seismic_data(long: float, lat: float, risk_category: str, site_class:str):
    try:
        data: dict[str, str | float] = {
            "longitude": long,
            "latitude": lat,
            "riskCategory": risk_category,
            "siteClass": site_class.upper()
        }

        # Use httpx.AsyncClient to make the non-blocking async request
        async with httpx.AsyncClient() as client:
            response = await client.get(USGC_URL, params=data)
            response.raise_for_status() 

            response_json = response.json()['response']

            if isinstance(response_json, str) and 'out of bounds' in response_json:
                return {'status': "error", 'detail':"Invalid long and lat. must be from USA"}

            response_data = response_json['data']
            return {
                'status':'success',
                'data':{
                    'sdc':response_data['sdc'],
                    'sds':response_data['sds'],
                    'sd1':response_data['sd1'],
                    's1':response_data['s1'],
                }
            }
    except Exception as e:
        logger.error(f"Error:{e}")
        return {'status': "error", 'detail':"Something Went Wrong during Seismic calc. Please try again later or ensure the lat/long are valid and based in USA"}


if __name__=='__main__':
    data = asyncio.run(get_seismic_data(long=-104.99, lat=39.74, risk_category='II', site_class='A'))
    print(data)