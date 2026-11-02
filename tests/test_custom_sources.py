import socket
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.database import Base
from app.models import User, CustomSource, SearchJob
import app.main  # Initialize template registry before importing routes.
from app.routers.sources import Configuration, create, update, delete
from app.services.site_renderer import check_url, PublicResolver


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.user = User(id=1, username='owner', password_hash='x', is_admin=False)
        self.db.add(self.user)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_save_permissions_and_delete_protection(self):
        config = Configuration(name='Example', search_url='https://example.com/?q=test',
            search_parameter='q', currency='EUR', selectors={'card':'.item','title':'.title','link':'a'})
        html = '<div class="item"><a href="/1" class="title">Laptop</a></div>'
        with patch('app.routers.sources.render_site', new=AsyncMock(return_value={'html':html,'url':config.search_url})):
            result = await create(config, self.db, self.user)
            other = User(id=2, username='other', password_hash='x', is_admin=False)
            with self.assertRaises(HTTPException) as error:
                await update(result['id'], config, self.db, other)
            self.assertEqual(error.exception.status_code, 403)
            self.db.add(SearchJob(user_id=1, name='Watch', platforms=[f"custom:{result['id']}"]))
            await self.db.commit()
            with self.assertRaises(HTTPException) as error:
                await delete(result['id'], self.db, self.user)
            self.assertEqual(error.exception.status_code, 409)

    async def test_network_guard(self):
        for url in ['http://127.0.0.1/', 'http://[::1]/', 'file:///etc/passwd', 'https://user:pass@example.com', 'http://example.com:8080']:
            with self.assertRaises(ValueError):
                check_url(url)
        check_url('https://example.com/search?q=test')
        loop = __import__('asyncio').get_running_loop()
        with patch.object(loop, 'getaddrinfo', new=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('10.0.0.1',80))])):
            with self.assertRaises(ValueError):
                await PublicResolver().resolve('example.com',80)
