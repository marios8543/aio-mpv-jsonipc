import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name='aio-mpv-jsonipc',
    version='0.3',
    author="marios8543",
    author_email="marios8543@gmail.com",
    description="Asyncio-compatible MPV JSON IPC client",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/marios8543/aio-mpv-jsonipc",
    packages=setuptools.find_packages(),
    py_modules=['aio_mpv_jsonipc.MPV'],
    classifiers=[
        "Programming Language :: Python :: 3",
         "License :: OSI Approved :: MIT License",
         "Operating System :: OS Independent",
    ]
)
