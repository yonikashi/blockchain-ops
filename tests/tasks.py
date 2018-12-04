"""Call various Terraform actions."""
from datetime import datetime, timezone
import os
import os.path
from time import sleep
from invoke import task
from invoke import exceptions


@task
def glide(c, version='v0.13.2'):
    """Download glide."""
    print('Downloading glide')

    # find glide version, copied from glide installation script
    os_name_res = c.run('uname|tr "[:upper:]" "[:lower:]"', hide='stdout')
    os_name = os_name_res.stdout.replace('\n', '')
    if os_name == 'linux':
        arch = 'linux-amd64'
    elif os_name == 'darwin':
        arch = 'darwin-amd64'
    else:
        raise exceptions.Failure(os_name_res, reason='Only supported on OSx and Linux')
    arch = os_name + '-' + arch
    print('Glide arch: {arch}'.format(arch=arch))

    # avoid redownloading file if exists
    if os.path.isfile('{cwd}/glide'.format(cwd=c.cwd)):
        print('Glide exists')
        return

    c.run('curl -sSLo glide.tar.gz https://github.com/Masterminds/glide/releases/download/{version}/glide-{version}-{arch}.tar.gz'.format(
        version=version, arch=arch), hide='stdout')
    c.run('tar -zxf ./glide.tar.gz', hide='stdout')
    c.run('mv ./{arch}/glide ./glide'.format(arch=arch), hide='stdout')
    c.run('rm -rf ./{arch} ./glide.tar.gz'.format(arch=arch), hide='stdout')

    print('Glide downloaded')


@task
def vendor(c):
    """Vendor go dependencies."""
    glide(c)

    print('Vendoring dependencies')
    c.run('./glide install', hide='stderr')


def is_image_exists(c, name):
    res = c.run('sudo docker images', hide='stdout')
    for image in res.stdout.split('\n'):
        if name in image:
            print('Image {name} exists'.format(name=name))
            return True

    print('Image {name} doesn\'t exist'.format(name=name))
    return False


@task
def build_core(c, version='kinecosystem/master'):
    """Build Core binary docker image."""
    with c.cd('images'):
        if is_image_exists(c, 'images_stellar-core'):
            return

        if not os.path.isdir('./images/volumes/stellar-core-git'):
            print('Core git repository doesn\'t exist, cloning')
            c.run('git clone --branch kinecosystem/master https://github.com/kinecosystem/stellar-core.git volumes/stellar-core-git')

        print('Building core')
        c.run('sudo docker-compose build stellar-core-build')
        c.run('sudo docker-compose run stellar-core-build')
        c.run('sudo docker-compose build stellar-core')


@task
def build_horizon(c, version='kinecosystem/master'):
    """Build Horizon binary docker image."""
    with c.cd('images'):
        if is_image_exists(c, 'images_horizon'):
            return

        if not os.path.isdir('./images/volumes/go-git'):
            print('Go (Horizon) Core git repository doesn\'t exist, cloning')
            c.run('git clone --branch kinecosystem/master https://github.com/kinecosystem/go.git volumes/go-git')

        with c.cd('volumes/go-git'):
            vendor(c)

        cmd = ' '.join([
            'bash', '-c',
            ('"go build -ldflags=\''
             '-X \\"github.com/kinecosystem/go/support/app.buildTime={timestamp}\\"'
             ' '
             '-X \\"github.com/kinecosystem/go/support/app.version={version}\\"'
             ' \''
             ).format(timestamp=datetime.now(timezone.utc).isoformat(), version=version),
            '-o', './horizon', './services/horizon"',
        ])

        print('Building Horizon')
        c.run('sudo docker-compose run horizon-build {cmd}'.format(cmd=cmd))
        c.run('sudo docker-compose build horizon')


@task
def rm_network(c):
    """Destroy local test network."""
    print('Stopping local test network and removing containers')
    with c.cd('images'):
        c.run('sudo docker-compose down -v', hide='stderr')

        c.run('sudo rm -rf volumes/stellar-core/opt/stellar-core/buckets')
        c.run('sudo rm -f volumes/stellar-core/opt/stellar-core/*.log')
        c.run('sudo rm -rf volumes/stellar-core/tmp')


@task(build_core, build_horizon, rm_network)
def network(c):
    """Initialize a new local test network with single core and horizon instances."""
    print('Launching local network')
    with c.cd('images'):
        print('Starting Core database')
        c.run('sudo docker-compose up -d stellar-core-db', hide='stderr')
        sleep(2)

        # setup core database
        # https://www.stellar.org/developers/stellar-core/software/commands.html
        print('Initializing Core database')
        res = c.run('sudo docker-compose run stellar-core --newdb --forcescp', hide='both')

        # fetch root account seed
        for line in res.stdout.split('\n'):
            if 'Root account seed' in line:
                root_account_seed = line.strip().split()[7]
                break

        print('Root account seed: {}'.format(root_account_seed))

        # setup cache history archive
        print('Initializing Core history archive')
        c.run('sudo docker-compose run stellar-core --newhist cache', hide='both')

        # start a local private testnet core
        # https://www.stellar.org/developers/stellar-core/software/testnet.html
        print('Starting Core')
        c.run('sudo docker-compose up -d stellar-core', hide='stderr')

        # setup horizon database
        print('Starting Horizon database')
        c.run('sudo docker-compose up -d horizon-db', hide='stderr')
        sleep(2)
        print('Initializing Horizon database')
        c.run('sudo docker-compose run horizon db init', hide='stderr')

        # start horizon
        # envsubst leaves windows carriage return "^M" artifacts when called by docker
        # this fixes it
        print('Starting Horizon')
        c.run('ROOT_ACCOUNT_SEED="{root_account_seed}" sudo -E docker-compose up -d horizon'.format(
            root_account_seed=root_account_seed), hide='stderr')

        #TODO: doesn't work, need to execute these commands manually
        # Update ledger parameters
        print('Updating base reserve')
        c.run('curl -s "localhost:11626/upgrades?mode=set&upgradetime=1970-01-01T00:00:00Z&basereserve=0"')
        c.run('curl -s "localhost:11626/upgrades?mode=set&upgradetime=1970-01-01T00:00:00Z&basereserve=0"')

        # Already starts at 9, but this fixes a bug that doesn't allow manageData ops
        print('Updating protocol version')
        c.run('curl -s "localhost:11626/upgrades?mode=set&upgradetime=1970-01-01T00:00:00Z&protocolversion=9"')
        c.run('curl -s "localhost:11626/upgrades?mode=set&upgradetime=1970-01-01T00:00:00Z&protocolversion=9"')

    print('Network ready')
