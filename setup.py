from setuptools import setup, find_packages

setup(
    name='l5x_agent_toolkit',
    version='0.1.0',
    description='AI Agent Toolkit for Rockwell Automation L5X file manipulation',
    packages=find_packages(),
    python_requires='>=3.9',
    install_requires=[
        'lxml>=4.9.0',
    ],
    extras_require={
        'dev': ['pytest>=7.0'],
        'mcp': ['mcp[cli]>=1.2.0'],
    },
    entry_points={
        'console_scripts': [
            'l5x-mcp-server=l5x_agent_toolkit.mcp_server:main',
        ],
    },
)
