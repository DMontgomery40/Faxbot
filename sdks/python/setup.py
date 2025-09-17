import io
import os
import setuptools


def read_readme() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    root_readme = os.path.abspath(os.path.join(here, os.pardir, os.pardir, "README.md"))
    try:
        with io.open(root_readme, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return "Client library for Faxbot API (send faxes via a unified API)."


setuptools.setup(
    name="faxbot",
    version="1.1.0",
    author="David Montgomery",
    author_email="dmontg@gmail.com",
    description="Faxbot API Client SDK for Python",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/DMontgomery40/Faxbot",
    packages=setuptools.find_packages(),
    install_requires=[
        "requests>=2.20.0",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Topic :: Communications :: Fax",
        "Intended Audience :: Developers",
    ],
    python_requires=">=3.7",
)
