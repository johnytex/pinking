import os
import base64
import logging
import argparse
import aiohttp
import json
import asyncio
from functools import partial
from pathlib import Path
from aiohttp import web
from multidict import MultiDict
from proxy import ipfs_proxy_handler

# TODO: fix this, already defined in pin.models.Pin but not sure how to import
PIN_TYPE_CHOICES = ['direct', 'recursive', 'indirect', 'mfs']

def _get_user_password(request):
    basic_auth = request.headers.get('Authorization', None)
    if basic_auth:
        user_pass_bytes = base64.b64decode(basic_auth.split(' ')[1])
        return user_pass_bytes.decode('utf-8').split(':')
    return None, None


async def _authenticate(request, django_url):
    #
    # Check with Django
    #

    user, pwd = _get_user_password(request)
    if user is None or pwd is None:
        return web.Response(status=401)
    logging.info(f'Authenticating with {user} {pwd}')
    return None


async def _files_handler(request, ipfs_url, django_url):
    """
    Handler for files requests (MFS). Authenticate user and then
    rewrite paths to keep within user sandbox
    """
    auth_response = await _authenticate(request, django_url)
    if auth_response is not None:
        return auth_response

    # Rewrite query paths
    username, pwd = _get_user_password(request)
    username_b64 = base64.b64encode(username.encode('utf-8')).decode('utf-8')
    new_query = MultiDict()
    for key, val in request.query.items():
        if key in ['arg', 'arg2'] and not val.startswith('/ipfs/'):
            new_path = f'/{username_b64}{os.path.normpath(val)}'
            new_query.add(key, new_path)
        else:
            new_query.add(key, val)

    return await ipfs_proxy_handler(request, ipfs_url, query=new_query)


async def _auth_handler(request, ipfs_url, django_url):
    """
    Handler for any request that just needs authentication and nothing more
    """
    auth_response = await _authenticate(request, django_url)
    if auth_response is not None:
        return auth_response

    return await ipfs_proxy_handler(request, ipfs_url)


async def _add_handler(request, ipfs_url, django_url):
    """
    Handler for any request that just needs authentication and nothing more
    """
    auth_response = await _authenticate(request, django_url)
    if auth_response is not None:
        return auth_response
    auth = request.headers['Authorization']

    # NOTE: don't write eof in the handler, since then the response would be over
    # but we still need to add the pins to django before we want to return
    response, body = await ipfs_proxy_handler(request, ipfs_url, return_body=True,
                                              write_eof=False)

    # Response can stream with progress=true, so get the last line which
    # contains the final hash
    last_line = body.decode('utf-8').strip().split('\n')[-1]
    multihash = json.loads(last_line)['Hash']

    if response.status == 200 and request.query.get('pin', 'true') == 'true':
        await _add_pins([multihash], 'recursive', ipfs_url, django_url, auth)

    # Now write eof
    await response.write_eof()
    return response


async def _pins_from_multihash(multihash, ipfs_url, pin_type, first=True):
    """
    Recursively applies `ipfs object links` to get all downstream hashes and
    their block sizes. Note: one could use `ipfs refs --recursive` but it
    is slower due to probably downloading the data, or traversing block hashes
    """
    refs = []
    stat_url = f'{ipfs_url}/api/v0/object/stat'
    links_url = f'{ipfs_url}/api/v0/object/links'
    resp = await app['session'].request('POST', stat_url, params={'arg': multihash})
    if resp.status != 200:
        return resp
    block_size = json.loads(await resp.text())['BlockSize']
    refs.append({'multihash': multihash, 'block_size': block_size,
                 'pin_type': pin_type if first else 'indirect'})

    resp = await app['session'].request('POST', links_url, params={'arg': multihash})

    if resp.status != 200:
        return resp

    # Recusively traverse children
    links = json.loads(await resp.text()).get('Links', None) or []
    for link in links:
        ret = await _pins_from_multihash(
            link['Hash'], ipfs_url, pin_type, first=False)
        if not isinstance(ret, list):
            return ret
        refs += ret

    return refs


async def _get_pins_django(django_url, auth, include_mfs=False):
    pins_url = f'{django_url}/api/pins/'
    headers = {'Authorization': auth}
    async with app['session'].request('GET', pins_url, headers=headers) as resp:
        if resp.status != 200:
            return resp
        pins = json.loads(await resp.text())

        # Massage pins to the ipfs api format
        out_pins = {}
        for pin in pins:
            pin_type = PIN_TYPE_CHOICES[pin['pin_type']]
            if pin_type == 'mfs' and not include_mfs:
                continue
            multihash = pin['multihash']
            if pin_type == 'indirect' and multihash in out_pins:
                continue # direct/recursive/mfs wins over indirect
            out_pins[multihash] = {'Type': pin_type}

        return {'Keys': out_pins}


async def _add_pins_django(pins, django_url, auth):
    for pin in pins:
        pin['pin_type'] = PIN_TYPE_CHOICES.index(pin['pin_type'])
    pins_url = f'{django_url}/api/pins/'
    headers = {'Authorization': auth}
    return await app['session'].request('POST', pins_url, headers=headers, json=pins)


async def _delete_pins_django(pins, django_url, auth):
    for pin in pins:
        if 'block_size' in pin:
            del pin['block_size']
        pin['pin_type'] = PIN_TYPE_CHOICES.index(pin['pin_type'])

    pins_url = f'{django_url}/api/delete-pins/'
    headers = {'Authorization': auth}
    return await app['session'].request('POST', pins_url, headers=headers, json=pins)


async def _pin_ls_handler(request, ipfs_url, django_url):
    """
    Get the pins from django/the database and return them
    """
    auth = request.headers['Authorization']
    ret = await _get_pins_django(django_url, auth)
    if not isinstance(ret, dict):
        return web.Response(status=ret.status, text=await ret.text())
    return web.json_response(ret)


async def _add_pins(multihash_args, pin_type, ipfs_url, django_url, auth):
    # Get pins from django
    ret = await _get_pins_django(django_url, auth)
    if not isinstance(ret, dict):
        return web.Response(status=ret.status, text=await ret.text())
    django_pins = ret

    # If any one of the pins fails, need to return error, and not pin any of
    # the other hashes
    for multihash in multihash_args:
        django_pin_type = (django_pins['Keys'][multihash]['Type']
                           if multihash in django_pins['Keys'] else None)
        if pin_type == 'direct' and django_pin_type == 'recursive':
            error_msg = {'Message': f'pin: {multihash} already pinned recursively',
                         'Code': 0}
            return web.json_response(error_msg, status=500)

    add_pins = []
    delete_pins = []
    for multihash in multihash_args:
        django_pin_type = (django_pins['Keys'][multihash]['Type']
                           if multihash in django_pins['Keys'] else None)
        if pin_type == django_pin_type:
            # Do nothing, just return the multihash
            pass
        else:
            ret = await _pins_from_multihash(multihash, ipfs_url, pin_type)
            if not isinstance(ret, list):
                return web.Response(status=ret.status, text=await ret.text())
            pins = ret
            if pin_type == 'recursive' and django_pin_type == 'direct':
                # If we're replacing a direct with recursive, delete the direct one
                delete_pins.append({'multihash': multihash, 'pin_type': 'direct'})
            add_pins += pins

    # Add new pins to ipfs as direct pins 
    pin_url = f'{ipfs_url}/api/v0/pin/add'
    pin_params = MultiDict()
    for pin in add_pins:
        pin_params['arg'] = pin['multihash']

    async with app['session'].request('POST', pin_url, params=pin_params) as resp:
        if resp.status != 200:
            return web.Response(status=resp.status, text=await resp.text())

    # Add new pins to django
    # NOTE: if this fails, we should roll back the direct pins we added to ipfs
    # but we can also let the garbage collector take care of it
    if len(add_pins) > 0:
        resp = await _add_pins_django(add_pins, django_url, auth)
        if resp.status != 201:
            return web.Response(status=resp.status, text=await resp.text())

    # Delete replaced pins (this will not affect disk usage, since duplicates
    # shouldn't count)
    if len(delete_pins) > 0:
        await _delete_pins_django(delete_pins, django_url, auth)


async def _pin_add_handler(request, ipfs_url, django_url):
    '''
    Recreates the ipfs pin logic
    TODO: move this logic into django
    '''
    multihash_args = request.query.getall('arg', [])
    pin_type = ('recursive' if request.query.get('recursive', 'true') == 'true'
                else 'direct')
    auth = request.headers['Authorization']
    ret = await _add_pins(multihash_args, pin_type, ipfs_url, django_url, auth)
    if ret is not None:
        return ret
    return web.json_response({'Pins': multihash_args})


async def _pin_rm_handler(request, ipfs_url, django_url):
    """
    Remove pins by first getting all user pins from django, then
    figure out the type of the pin to be removed and which indirect (if any)
    pins that should also be removed
    """
    multihash_args = request.query.getall('arg', [])

    auth = request.headers['Authorization']
    ret = await _get_pins_django(django_url, auth)
    if not isinstance(ret, dict):
        return web.Response(status=ret.status, text=await ret.text())
    django_pins = ret
    
    delete_pins = []
    for multihash in multihash_args:
        if multihash not in django_pins['Keys']:
            return web.json_response({'Message': 'not pinned', 'Code': 0}, status=500)
        pin_type_django = django_pins['Keys'][multihash]['Type']
        if pin_type_django == 'recursive':
            ret = await _pins_from_multihash(multihash, ipfs_url, pin_type_django)
            if not isinstance(ret, list):
                return web.Response(status=ret.status, text=await ret.text())
            delete_pins += ret
        elif pin_type_django == 'direct':
            delete_pins.append({'multihash': multihash, 'pin_type': pin_type_django})
        else:
            # TODO: The real message from IPFS is:
            # "{h1} is pinned indirectly under {h2}"
            # We don't currently store which recursive pin owns which indirect pin
            # though, so for now:
            msg = {
                'Message': f'{multihash} is pinned indirectly under another pin',
                'Code': 0
            }
            return web.json_response(msg, status=500)
    
    if len(delete_pins) > 0:
        await _delete_pins_django(delete_pins, django_url, auth)

    return web.json_response({'Pins': multihash_args})


async def _pin_update_handler(request, ipfs_url, django_url):
    pass


async def _pin_verify_handler(request, ipfs_url, django_url):
    return web.Response(status=501, text='Error: this api is not implemented by pinking')


async def _on_startup(app):
    app['session'] = aiohttp.ClientSession()


async def _on_cleanup(app):
    await app['session'].close()


if __name__ == "__main__":
    lvl_map = {
        'DEBUG': logging.DEBUG, 'INFO': logging.INFO, 'WARNING': logging.WARNING,
        'ERROR': logging.ERROR, 'CRITICAL': logging.CRITICAL
    }
    parser = argparse.ArgumentParser(description='Run the pinking server proxy')
    parser.add_argument("--listen_port", help="set the listening port",
                        type=int, default=5002)
    parser.add_argument("--ipfs_port", help="set the ipfs port",
                        type=int, default=5001)
    parser.add_argument("--django_port", help="set the django port",
                        type=int, default=8000)
    parser.add_argument("--logfile", help="the optional output log file", type=str)
    parser.add_argument("--loglvl", help="the log level",
                        type=str, choices=list(lvl_map.keys()), default='INFO')
    parser.add_argument("--ssl_cert_path", help="use ssl certs at path", type=str)
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(format='server proxy: %(asctime)s %(message)s',
                        datefmt='%m/%d/%Y %I:%M:%S %p',
                        level=args.loglvl,
                        filename=args.logfile)

    if args.ssl_cert_path is not None and not os.path.exists(args.ssl_cert_path):
        print(f'SSL cert path {ssl_cert_path} doesn\'t exist')
        raise ValueError()

    ssl_context = None
    if args.ssl_cert_path is not None:
        ssl_cert_path = Path(args.ssl_cert_path)
        if not args.ssl_cert_path.exists():
            print(f'SSL cert path {ssl_cert_path} doesn\'t exist')
            raise ValueError()

        print(f'Trying to use ssl context from {ssl_cert_path}')
        fullchain_path = ssl_cert_path / 'fullchain.pem'
        privkey_path = ssl_cert_path / 'privkey.pem'
        if fullchain_path.exists() and privkey_path.exists():
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
            ssl_context.load_cert_chain(fullchain_path, privkey_path)
        else:
            print('Fullchain or privkey didn\'t exist')
            raise ValueError()

    ipfs_url = f'http://127.0.0.1:{args.ipfs_port}'
    django_url = f'http://127.0.0.1:{args.django_port}'
    kwargs = {'ipfs_url': ipfs_url, 'django_url': django_url}
    app = web.Application()
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    routes = []
    # -----
    # Misc
    # -----
    add_handler = partial(_add_handler, **kwargs)
    routes.append((['add'], partial(_add_handler, **kwargs)))

    # -----------------------------
    # Commands requiring auth only
    # -----------------------------
    auth_handler = partial(_auth_handler, **kwargs)
    auth_commands = ['cat', 'get', 'ls', 'refs', 'object/data',
                     'object/diff', 'object/get', 'object/links', 'object/new',
                     'object/patch/add-link', 'object/patch/append-data',
                     'object/patch/rm-link', 'object/patch/set-data',
                     'object/put', 'object/stat', 'version', 'tar/add',
                     'tar/cat']
    routes.append((auth_commands, auth_handler))

    # ----------
    # ipfs files
    # ----------
    files_handler = partial(_files_handler, **kwargs)
    files_commands = ['chcid', 'cp', 'flush', 'ls', 'mkdir', 'mv', 'read',
                      'rm', 'stat', 'write']
    files_commands = [f'files/{command}' for command in files_commands]
    routes.append((files_commands, files_handler))

    # ---------
    # ipfs key
    # ---------
    '''
    key_handler = partial(_key_handler, ipfs_url=ipfs_url)
    routes.append(['key/gen'], _key_gen_handler)
    routes.append(['key/list'], _key_list_handler)
    routes.append(['key/rename'], _key_rename_handler)
    routes.append(['key/rm'], _key_rm_handler)
    '''

    # ---------
    # ipfs pin
    # ---------
    routes.append((['pin/ls'], partial(_pin_ls_handler, **kwargs)))
    routes.append((['pin/add'], partial(_pin_add_handler, **kwargs)))
    routes.append((['pin/rm'], partial(_pin_rm_handler, **kwargs)))
    routes.append((['pin/update'], partial(_pin_update_handler, **kwargs)))
    routes.append((['pin/verify'], partial(_pin_verify_handler, **kwargs)))
    for paths, handler in routes:
        for path in paths:
            app.router.add_route('POST', f'/api/v0/{path}', handler)

    try:
        web.run_app(app, host='0.0.0.0', ssl_context=ssl_context, port=args.listen_port)
    except KeyboardInterrupt:
        pass
