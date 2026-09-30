"""Read existing review records using the SDK; never index, activate or publish automatically."""

import argparse
import asyncio
import os

from sio_sdk import SioClient


async def inspect(url: str, token: str, video_id: str | None) -> None:
    async with SioClient(url=url, token=token) as client:
        review = client.investigations
        library = await review.videos()
        print(f"Accessible recordings: {len(library.videos)}")
        timeline = await review.timeline()
        print(
            f"Timeline page: {len(timeline.recordings)} recordings; more={bool(timeline.next_cursor)}"
        )
        if video_id:
            page = await review.analyses(video_id)
            for analysis in page.analyses:
                print(analysis.analysis_id, analysis.status)
            print(f"Older analysis page available: {bool(page.next_cursor)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--video-id")
    arguments = parser.parse_args()
    token = os.environ.get("SIO_TOKEN")
    if not token:
        parser.error("Set SIO_TOKEN to an authorized access token before contacting the API")
    asyncio.run(inspect(arguments.url, token, arguments.video_id))


if __name__ == "__main__":
    main()
