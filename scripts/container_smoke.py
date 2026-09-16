"""Run against the disposable CI container only (127.0.0.1:17779)."""
import base64
import httpx

with httpx.Client(base_url='http://127.0.0.1:17779', timeout=180) as client:
    assert client.get('/api/ready').status_code == 200
    assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'}).status_code == 200
    settings = client.get('/api/settings').json()
    settings.update(scheduler_enabled=False, health_check_enabled=False)
    assert client.put('/api/settings', json=settings).status_code == 200
    clients = client.get('/api/client-types').json()
    user = base64.urlsafe_b64encode(b'aes-128-gcm:fixture-password').decode().rstrip('=')
    group = client.post('/api/subscriptions', json={
        'name': 'CI isolated renderer probe',
        'manual_content': f'ss://{user}@node.example.invalid:443#fixture',
        'outputs': [{'name': f'中文订阅 🚀 {kind}', 'slug': kind, 'client_type': kind} for kind in clients],
    })
    group.raise_for_status()
    gid = group.json()['id']
    refreshed = client.post(f'/api/subscriptions/{gid}/refresh').json()
    failed = [x['name'] for x in refreshed.get('outputs', []) if not x['ok']]
    assert refreshed.get('success') == len(clients), f'Failed renderer client types: {failed}'
    urls = client.get(f'/api/subscriptions/{gid}/public-urls').json()['urls']
    for output in urls:
        response = client.get(output['url'])
        assert response.status_code == 200 and response.content, output['client_type']
        title = response.headers['profile-title']
        assert title.startswith('base64:')
        assert base64.b64decode(title[7:]).decode('utf-8') == f"中文订阅 🚀 {output['client_type']}"
        download = client.get(output['url'], params={'download': '1'})
        assert download.status_code == 200 and download.content == response.content
        assert download.headers['content-disposition'].startswith('attachment;')
    assert client.delete(f'/api/subscriptions/{gid}').status_code == 200
print('Readiness, all configured renderer types and public URLs passed')
