import argparse
import logging
import os

import gzip
import tarfile
import shutil
import git
import shell
import yaml
from git import RemoteProgress
from tqdm import tqdm
from yamlinclude import YamlIncludeConstructor

__version__ = '0.1 beta'


class CloneProgress(RemoteProgress):
    def __init__(self):
        super().__init__()
        self.pbar = tqdm(bar_format='[INFO]: {percentage:3.0f}% | {bar}', ncols=80)

    def update(self, op_code, cur_count, max_count=None, message=''):
        self.pbar.total = max_count
        self.pbar.n = cur_count
        self.pbar.refresh()


def parse_commandline():
    parser = argparse.ArgumentParser(
        prog='vpd-build-release',
        usage="./vpd-build-release [OPTION] filename",
        description="Pack releases for VPD",
        epilog="(c) STM Labs 2023")
    parser.add_argument('filename', help='release configuration in YAML format')
    parser.add_argument('-V', '--version', action='version', version='%(prog)s ' + __version__)
    return parser.parse_args()


def print_array(config):
    for item in config:
        logging.info(item)


def load_config(filename_str: str):
    logging.info(f"Load config from {filename_str}")
    YamlIncludeConstructor.add_to_loader_class(loader_class=yaml.FullLoader, base_dir=os.path.curdir)
    with open(filename_str) as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)

    return cfg


def tar_gzip(uncompressed: str):
    compressed = f"{uncompressed}.tar.gz"
    logging.info(f"Tar.gzip {uncompressed} to {compressed}")
    with tarfile.open(compressed, mode="x:gz") as f_out:
        f_out.add(uncompressed, recursive=True)


def my_gzip(uncompressed: str):
    compressed = f"{uncompressed}.gz"
    logging.info(f"Gzip {uncompressed} to {compressed}")
    with open(uncompressed, 'rb') as f_in:
        with gzip.open(compressed, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)


def clone_charts(charts_dict: dict):
    name = charts_dict['name']
    protocol = charts_dict['protocol']
    registry = charts_dict['registry']
    login = charts['login'] or os.environ['GIT_LOGIN']
    token = charts['token'] or os.environ['GIT_TOKEN']
    branch = charts_dict['branch']
    git_url = f"{protocol}://{login}:{token}@{registry}/{name}.git"

    logging.info(f"Clone charts: \"{branch}\" branch from {protocol}://{registry}/{name}.git to {name}")
    git.Repo.clone_from(git_url, os.path.join(name), branch=branch)  # progress=CloneProgress()


def patch_charts(charts_dict: dict, services_dict: dict, *, inner_folder: str = False):
    charts_renamed_str: str = charts_dict['rename_to']
    if inner_folder:  # Если чарты для нескольких микросервисов/релизов
        for service in services_dict:
            path_to_values_yaml = os.path.join(charts_renamed_str, inner_folder, 'charts',
                                               service['name'], "values.yaml")
            patch_values_yaml(path_to_values_yaml, service)

    else:  # если чарты для одного микросервиса
        path_to_values_yaml = os.path.join(charts_renamed_str, 'charts', services_dict['name'], "values.yaml")
        patch_values_yaml(path_to_values_yaml, services_dict)


def patch_values_yaml(path_to_values_yaml: str, service: dict):
    with open(path_to_values_yaml, 'r') as values_yaml:
        values_yaml_in_memory = []
        while line := values_yaml.readline():
            if line.find('pre-ci') >= 0:
                line = line.replace('pre-ci', str(service['build']))
            values_yaml_in_memory.append(line)
    with open(path_to_values_yaml, 'w') as values_yaml:
        values_yaml.writelines(values_yaml_in_memory)


def pull_images(registry: str, images_dict: dict, build_num: int):
    pulled_images_list = []
    for image in images_dict['download']:
        image_fqn = f"{registry}/{image['name']}:{build_num}"
        logging.info(f"Pull image {image_fqn}")
        shell.shell(f"docker pull {image_fqn} --quiet")
        pulled_images_list.append(image_fqn)
    return pulled_images_list


def tag_images(images_list: [], new_registry: str):
    tagged_images_str = ""
    for image in images_list:
        logging.info(f"Tag {image} for {new_registry}")
        image_split_to_list = image.rsplit('/')
        new_tag = new_registry + "/" + image_split_to_list[-1]
        shell.shell(f"docker tag {image} {new_tag}")
        tagged_images_str = tagged_images_str + new_tag + " "
    return tagged_images_str


def save_images(images_to_save: str, archive_str: str):
    tar_images_archive = f"{archive_str}.tar"

    logging.info(f"Save images to {archive_str}.tar")
    shell.shell(f"docker save {images_to_save} --output={tar_images_archive}")

    my_gzip(tar_images_archive)

    logging.info(f"Remove {tar_images_archive}")
    os.remove(os.path.join(tar_images_archive))


def split_images_archive(archives: dict):
    source_filename = os.path.join(os.path.curdir, f"{archives['images']}.tar.gz")
    part_size_bytes = archives['split'] * 1024 * 1024
    if os.stat(source_filename).st_size < part_size_bytes:
        logging.info(f"File {source_filename} is less than {archives['split']}Mb")
        return
    logging.info(f"Split {source_filename} by {archives['split']}Mb")
    part_number = 0
    with open(source_filename, 'rb') as input_file:
        while True:
            chunk = input_file.read(part_size_bytes)
            if not chunk:
                break
            part_number += 1
            split_filename = f"{source_filename}-{part_number:02.0f}"
            with open(os.path.join(os.path.curdir, split_filename), 'wb') as destination_file:
                logging.info(f"Write part: {os.path.split(split_filename)[-1]}")
                destination_file.write(chunk)
    if part_number != 0:
        logging.info(f"Remove {source_filename}")
        os.remove(source_filename)
    return part_number


def build_release(name):
    pass


def patch_services(services_dict: dict, default_build: int):
    temp_dict = []
    for service in services_dict:
        if not ('build' in service):
            service['build'] = default_build
        temp_dict.append(service)
    return temp_dict


if __name__ == '__main__':
    # Конфигурируем логирование
    logging.basicConfig(format='[%(levelname)s]: %(message)s',
                        encoding='utf-8',
                        level=logging.INFO)

    # Разбираем параметры командной строки
    commandline_parameters = parse_commandline()

    # Загружаем конфиг из указанного файла
    config = load_config(commandline_parameters.filename)

    # Разбираем загруженную конфигурацию на переменные
    # для удобства использования
    build = config['build']
    charts = config['charts']
    archives = config['archives']
    charts_name = charts['name']
    charts_renamed = f"{config['name']}-charts"
    services = config['services']
    images = config['images']
    registries = config['docker']['registries']
    registry_local_url = registries['local']['url']
    registry_production_url = registries['production']['url']

    # Получаем чарты с git-репозитория
    clone_charts(charts)

    # Обогащение services дефолтным номером сборки там, где он не проставлен
    services = patch_services(services, build)

    # Приводим имя папки чартов к виду "release-name-charts"
    logging.info(f"Rename {charts_name} to {charts_renamed}")
    os.rename(charts_name, charts_renamed)

    # Патчим номера сборок в чартах (pre-ci => Drone CI build number)
    patch_charts(charts, services, inner_folder=charts['inner_folder'])

    # Очищаем чарты от БД гита, на проде она не нужна
    logging.info("Clean .git database from charts")
    shutil.rmtree(f"{charts_renamed}/.git", ignore_errors=True)

    # Архивируем чарты в .tar.gz
    tar_gzip(charts_renamed)
    shutil.rmtree(charts_renamed, ignore_errors=True)

    images_to_pull = scan_charts_for_images()
    # Сохраняем docker-образы из images.download
    pulled_images: list = pull_images(registry_local_url, images, build)

    # Проставляем теги, необходимые для загрузки этих образов в продовую docker registry
    tagged_images: str = tag_images(pulled_images, registry_production_url)

    # Сохраняем протеженные docker-образы в общий .tar.gz-архив
    save_images(tagged_images, archives['images'])

    # Если .tar.gz-архив получился больше 200 Мб,
    # то разбиваем его на части с суффиксами -XX, нумерация с 1
    split_images_archive(archives)

    # Объединяем архивы в итоговую поставку
    build_release(config)
