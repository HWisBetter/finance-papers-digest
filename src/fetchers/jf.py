"""Journal of Finance（Wiley，在线 ISSN 15406261）。

JF 的抓取逻辑已抽到通用的 wiley.py（JF/JAR 等共用）；这里只留薄封装，
保持 `fetch_jf()` 这个老接口可用，方便单独自测与历史引用。
"""

from src.fetchers.wiley import fetch_wiley

JF_ISSN = "15406261"


def fetch_jf() -> list[dict]:
    return fetch_wiley(JF_ISSN, "JF")


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s | %(message)s")
    result = fetch_jf()
    print(f"\n共抓到 {len(result)} 篇，前 3 篇预览：\n")
    for p in result[:3]:
        print("id      :", p["id"])
        print("title   :", p["title"])
        print("authors :", p["authors"])
        print("url     :", p["url"])
        print("pub_date:", p["published_date"])
        print("source  :", p["source"])
        print("abstract:", p["abstract"][:200], "...")
        print("-" * 70)
