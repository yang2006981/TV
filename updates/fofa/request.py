from utils.config import config, resource_path
from tqdm.asyncio import tqdm_asyncio
from time import time
from requests import get
from concurrent.futures import ThreadPoolExecutor, as_completed
import updates.fofa.fofa_map as fofa_map
from driver.setup import setup_driver
import re
from utils.retry import retry_func
from utils.channel import format_channel_name
from utils.tools import merge_objects, get_pbar_remaining
from updates.proxy import get_proxy, get_proxy_next
from requests_custom.utils import get_source_requests, close_session
from collections import defaultdict
import pickle
import threading

timeout = config.getint("Settings", "request_timeout", fallback=10)


def get_fofa_urls_from_region_list():
    """
    Get the FOFA url from region
    """
    region_list = [
        region.strip()
        for region in config.get(
            "Settings", "hotel_region_list", fallback="全部"
        ).split(",")
        if region.strip()
    ]
    urls = []
    region_url = getattr(fofa_map, "region_url")
    if "all" in region_list or "ALL" in region_list or "全部" in region_list:
        urls = [url for url_list in region_url.values() for url in url_list if url]
    else:
        for region in region_list:
            if region in region_url:
                urls.append(region_url[region])
    return urls


def update_fofa_region_result_tmp(result, multicast=False):
    """
    Update fofa region result tmp
    """
    tmp_result = get_fofa_region_result_tmp(multicast=multicast)
    total_result = merge_objects(tmp_result, result)
    with open(
        resource_path(
            f"updates/fofa/fofa_{'multicast' if multicast else 'hotel'}_region_result.pkl"
        ),
        "wb",
    ) as file:
        pickle.dump(total_result, file)


def get_fofa_region_result_tmp(multicast: False):
    with open(
        resource_path(
            f"updates/fofa/fofa_{'multicast' if multicast else 'hotel'}_region_result.pkl"
        ),
        "rb",
    ) as file:
        result = pickle.load(file)
        return result


async def get_channels_by_fofa(urls=None, multicast=False, callback=None):
    """
    Get the channel by FOFA
    """
    fofa_urls = urls if urls else get_fofa_urls_from_region_list()
    fofa_urls_len = len(fofa_urls)
    pbar = tqdm_asyncio(
        total=fofa_urls_len,
        desc=f"Processing fofa {'for multicast' if multicast else 'for hotel'}",
    )
    start_time = time()
    fofa_results = {}
    mode_name = "组播" if multicast else "酒店"
    if callback:
        callback(
            f"正在获取Fofa{mode_name}源, 共{fofa_urls_len}个查询地址",
            0,
        )
    proxy = None
    open_proxy = config.getboolean("Settings", "open_proxy", fallback=False)
    open_driver = config.getboolean("Settings", "open_driver", fallback=True)
    open_sort = config.getboolean("Settings", "open_sort", fallback=True)
    if open_proxy:
        test_url = fofa_urls[0][0] if multicast else fofa_urls[0]
        proxy = await get_proxy(test_url, best=True, with_test=True)
    cancel_event = threading.Event()

    def process_fofa_channels(fofa_info):
        nonlocal proxy, fofa_urls_len, open_driver, open_sort, cancel_event
        if cancel_event.is_set():
            return {}
        fofa_url = fofa_info[0] if multicast else fofa_info
        results = defaultdict(lambda: defaultdict(list))
        driver = None
        try:
            if open_driver:
                driver = setup_driver(proxy)
                try:
                    retry_func(lambda: driver.get(fofa_url), name=fofa_url)
                except Exception as e:
                    if open_proxy:
                        proxy = get_proxy_next()
                    driver.close()
                    driver.quit()
                    driver = setup_driver(proxy)
                    driver.get(fofa_url)
                page_source = driver.page_source
            else:
                page_source = retry_func(
                    lambda: get_source_requests(fofa_url), name=fofa_url
                )
            if "资源访问每天限制" in page_source:
                cancel_event.set()
                raise ValueError("Limited access to fofa page")
            fofa_source = re.sub(r"<!--.*?-->", "", page_source, flags=re.DOTALL)
            urls = set(re.findall(r"https?://[\w\.-]+:\d+", fofa_source))
            if multicast:
                region = fofa_info[1]
                type = fofa_info[2]
                multicast_result = [(url, None, None) for url in urls]
                results[region][type] = multicast_result
            else:
                with ThreadPoolExecutor(max_workers=100) as executor:
                    futures = [
                        executor.submit(process_fofa_json_url, url, open_sort)
                        for url in urls
                    ]
                    for future in futures:
                        results = merge_objects(results, future.result())
            return results
        except ValueError as e:
            raise e
        except Exception as e:
            print(e)
        finally:
            if driver:
                driver.close()
                driver.quit()
            pbar.update()
            remain = fofa_urls_len - pbar.n
            if callback:
                callback(
                    f"正在获取Fofa{mode_name}源, 剩余{remain}个查询地址待获取, 预计剩余时间: {get_pbar_remaining(n=pbar.n, total=pbar.total, start_time=start_time)}",
                    int((pbar.n / fofa_urls_len) * 100),
                )

    max_workers = 3 if open_driver else 10
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(process_fofa_channels, fofa_url) for fofa_url in fofa_urls
        ]
        try:
            for future in as_completed(futures):
                result = future.result()
                if result:
                    fofa_results = merge_objects(fofa_results, result)
        except ValueError as e:
            if "Limited access to fofa page" in str(e):
                for future in futures:
                    future.cancel()
    if fofa_results:
        update_fofa_region_result_tmp(fofa_results, multicast=multicast)
    else:
        fofa_results = get_fofa_region_result_tmp(multicast=multicast)
        pbar.n = fofa_urls_len
        pbar.update(0)
        if callback:
            callback(
                f"正在获取Fofa{mode_name}源",
                100,
            )
    if not open_driver:
        close_session()
    pbar.close()
    return fofa_results


def process_fofa_json_url(url, open_sort):
    """
    Process the FOFA json url
    """
    channels = {}
    try:
        final_url = url + "/iptv/live/1000.json?key=txiptv"
        # response = retry_func(
        #     lambda: get(final_url, timeout=timeout),
        #     name=final_url,
        # )
        response = get(final_url, timeout=timeout)
        try:
            json_data = response.json()
            if json_data["code"] == 0:
                try:
                    for item in json_data["data"]:
                        if isinstance(item, dict):
                            item_name = format_channel_name(item.get("name"))
                            item_url = item.get("url").strip()
                            if item_name and item_url:
                                total_url = (
                                    f"{url}{item_url}$cache:{url}"
                                    if open_sort
                                    else f"{url}{item_url}"
                                )
                                if item_name not in channels:
                                    channels[item_name] = [(total_url, None, None)]
                                else:
                                    channels[item_name].append((total_url, None, None))
                except Exception as e:
                    # print(f"Error on fofa: {e}")
                    pass
        except Exception as e:
            # print(f"{url}: {e}")
            pass
    except Exception as e:
        # print(f"{url}: {e}")
        pass
    finally:
        return channels
