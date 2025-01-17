""" Geofabrik data extracts http://download.geofabrik.de/ """

import copy
import os
import re
import time
import urllib.error
import urllib.parse

import bs4
import fuzzywuzzy.process
import humanfriendly
import numpy as np
import pandas as pd
import requests

from pydriosm.utils import cd_dat, cd_dat_geofabrik, download, load_json, load_pickle, save_json, save_pickle
from pydriosm.utils import confirmed, regulate_input_data_dir, update_nested_dict


# Get raw directory table (allowing us to check logs of older files and their and download links)
def get_raw_directory_table(url):
    """
    :param url: [str] URL, e.g. 'https://download.geofabrik.de/europe/great-britain.html'
    :return: [pandas.DataFrame] raw directory index table
    """
    assert isinstance(url, str)
    if url.endswith('.osm.pbf') or url.endswith('.shp.zip') or url.endswith('.osm.bz2'):
        print("Failed to get the requested information due to invalid input URL.")
        raw_directory_index = None
    else:
        try:
            raw_directory_index = pd.read_html(url, match='file', header=0, parse_dates=['date'])
            raw_directory_index = pd.DataFrame(pd.concat(raw_directory_index, axis=0, ignore_index=True))
            raw_directory_index.columns = [c.title() for c in raw_directory_index.columns]

            # Clean the DataFrame
            raw_directory_index.Size = raw_directory_index.Size.apply(humanfriendly.format_size)
            raw_directory_index.sort_values('Date', ascending=False, inplace=True)
            raw_directory_index.index = range(len(raw_directory_index))

            raw_directory_index['FileURL'] = raw_directory_index.File.map(lambda x: urllib.parse.urljoin(url, x))

        except (urllib.error.HTTPError, TypeError, ValueError):
            if len(urllib.parse.urlparse(url).path) <= 1:
                print("The home page does not have a raw directory index.")
            raw_directory_index = None

    return raw_directory_index


# For a given URL, get a table containing all available URLs for downloading each subregion's OSM data
def get_subregion_table(url):
    """
    :param url: [str] URL, e.g. 'https://download.geofabrik.de/europe/great-britain.html'
    :return: [pandas.DataFrame] a table of all available subregions' URLs
    """
    assert isinstance(url, str)
    if url.endswith('.osm.pbf') or url.endswith('.shp.zip') or url.endswith('.osm.bz2'):
        print("Failed to get the requested information due to invalid input URL.")
        subregion_table = None
    else:
        try:
            subregion_table = pd.read_html(url, match=re.compile(r'(Special )?Sub[ \-]Regions?'), encoding='UTF-8')
            subregion_table = pd.DataFrame(pd.concat(subregion_table, axis=0, ignore_index=True))

            # Specify column names
            file_types = ['.osm.pbf', '.shp.zip', '.osm.bz2']
            column_names = ['Subregion'] + file_types
            column_names.insert(2, '.osm.pbf_Size')

            # Add column/names
            if len(subregion_table.columns) == 4:
                subregion_table.insert(2, '.osm.pbf_Size', np.nan)
            subregion_table.columns = column_names

            subregion_table.replace({'.osm.pbf_Size': {re.compile('[()]'): '', re.compile('\xa0'): ' '}}, inplace=True)

            # Get the URLs
            source = requests.get(url)
            soup = bs4.BeautifulSoup(source.text, 'lxml')
            source.close()

            for file_type in file_types:
                text = '[{}]'.format(file_type)
                urls = [urllib.parse.urljoin(url, link['href']) for link in
                        soup.find_all(name='a', href=True, text=text)]
                subregion_table.loc[subregion_table[file_type].notnull(), file_type] = urls

            try:
                subregion_urls = [urllib.parse.urljoin(url, soup.find('a', text=text)['href']) for text in
                                  subregion_table.Subregion]
            except TypeError:
                subregion_urls = [kml['onmouseover'] for kml in soup.find_all('tr', onmouseover=True)]
                subregion_urls = [s[s.find('(') + 1:s.find(')')][1:-1].replace('kml', 'html') for s in subregion_urls]
                subregion_urls = [urllib.parse.urljoin(url, sub_url) for sub_url in subregion_urls]
            subregion_table['SubregionURL'] = subregion_urls

            column_names = list(subregion_table.columns)
            column_names.insert(1, column_names.pop(len(column_names) - 1))
            subregion_table = subregion_table[column_names]  # .fillna(value='')

        except (ValueError, TypeError, ConnectionRefusedError, ConnectionError):
            # No more data available for subregions within the region
            print("Checked out \"{}\".".format(url.split('/')[-1].split('.')[0].title()))
            subregion_table = None

    return subregion_table


# Scan through the downloading pages to collect catalogues of subregion information
def collect_subregion_info_catalogue(confirmation_required=True):
    """
    :param confirmation_required: [bool] whether to ask for a confirmation before starting to collect information
    """
    if confirmed("To collect all available subregion links? (Note that it may take a few minutes.) ",
                 confirmation_required=confirmation_required):

        home_url = 'http://download.geofabrik.de/'

        try:
            source = requests.get(home_url)
            soup = bs4.BeautifulSoup(source.text, 'lxml')
            source.close()
            avail_subregions = [td.a.text for td in soup.find_all('td', {'class': 'subregion'})]
            avail_subregion_urls = [urllib.parse.urljoin(home_url, td.a['href']) for td in
                                    soup.find_all('td', {'class': 'subregion'})]
            avail_subregion_url_tables = [get_subregion_table(sub_url) for sub_url in avail_subregion_urls]
            avail_subregion_url_tables = [tbl for tbl in avail_subregion_url_tables if tbl is not None]

            subregion_url_tables = list(avail_subregion_url_tables)

            while subregion_url_tables:

                subregion_url_tables_ = []

                for subregion_url_table in subregion_url_tables:
                    subregions = list(subregion_url_table.Subregion)
                    subregion_urls = list(subregion_url_table.SubregionURL)
                    subregion_url_tables_0 = [get_subregion_table(subregion_url) for subregion_url in subregion_urls]
                    subregion_url_tables_ += [tbl for tbl in subregion_url_tables_0 if tbl is not None]

                    # (Note that 'Russian Federation' data is available in both 'Asia' and 'Europe')
                    avail_subregions += subregions
                    avail_subregion_urls += subregion_urls
                    avail_subregion_url_tables += subregion_url_tables_

                subregion_url_tables = list(subregion_url_tables_)

            # Save a list of available subregions locally
            save_pickle(avail_subregions, cd_dat("GeoFabrik-subregion-name-list.pickle"))

            # Subregion index - {Subregion: URL}
            subregion_url_index = dict(zip(avail_subregions, avail_subregion_urls))
            # Save subregion_index to local disk
            save_pickle(subregion_url_index, cd_dat("GeoFabrik-subregion-name-url-dictionary.pickle"))
            save_json(subregion_url_index, cd_dat("GeoFabrik-subregion-name-url-dictionary.json"))

            # All available URLs for downloading
            home_subregion_url_table = get_subregion_table(home_url)
            avail_subregion_url_tables.append(home_subregion_url_table)
            subregion_downloads_index = pd.DataFrame(pd.concat(avail_subregion_url_tables, ignore_index=True))
            subregion_downloads_index.drop_duplicates(inplace=True)
            subregion_downloads_index_json = subregion_downloads_index.set_index('Subregion').to_json()

            # Save subregion_index_downloads to local disk
            save_pickle(subregion_downloads_index, cd_dat("GeoFabrik-subregion-downloads-catalogue.pickle"))
            save_json(subregion_downloads_index_json, cd_dat("GeoFabrik-subregion-downloads-catalogue.json"))

        except Exception as e:
            print("Failed to get the required information ... {}.".format(e))

    else:
        print("The information collection process was not activated.")


# Fetch a requested catalogue of subregion information
def fetch_subregion_info_catalogue(catalogue_name, file_format=".pickle", update=False):
    """
    :param catalogue_name: [str] e.g. "GeoFabrik-subregion-name-list"
    :param file_format: [str] ".pickle"(default), or ".json"
    :param update: [bool] whether to update (re-collect) the catalogues of subregion information
    :return: [list, dict, pandas.DataFrame] or null
    """
    available_catalogue = ("GeoFabrik-subregion-name-list",
                           "GeoFabrik-subregion-name-url-dictionary",
                           "GeoFabrik-subregion-downloads-catalogue")
    assert catalogue_name in available_catalogue, \
        "'catalogue_name' must be one of the following: \n  \"{}\".".format("\",\n  \"".join(available_catalogue))

    available_fmt = (".pickle", ".json")
    assert file_format in available_fmt, \
        "'file_format' must be one of the following: \n  \"{}\".".format("\",\n  \"".join(available_fmt))

    path_to_catalogue = cd_dat(catalogue_name + file_format)
    if not os.path.isfile(path_to_catalogue) or update:  # all(paths_to_files_exist) and
        collect_subregion_info_catalogue(confirmation_required=True)
    try:
        index_file = load_pickle(path_to_catalogue) if file_format == ".pickle" else load_json(path_to_catalogue)
        return index_file
    except Exception as e:
        print(e)


# Scan through the home page to collect information about subregions for each continent
def collect_continents_subregion_tables(confirmation_required=True):
    """
    :param confirmation_required: [bool] whether to ask for a confirmation before starting to collect the information
    """
    if confirmed("To collect information about subregions of each continent? ",
                 confirmation_required=confirmation_required):
        try:
            home_link = 'https://download.geofabrik.de/'
            source = requests.get(home_link)
            soup = bs4.BeautifulSoup(source.text, 'lxml').find_all('td', {'class': 'subregion'})
            source.close()
            continent_names = [td.a.text for td in soup]
            continent_links = [urllib.parse.urljoin(home_link, td.a['href']) for td in soup]
            subregion_tables = dict(zip(continent_names, [get_subregion_table(url) for url in continent_links]))
            save_pickle(subregion_tables, cd_dat("GeoFabrik-continents-subregion-tables.pickle"))
        except Exception as e:
            print("Failed to collect the required information ... {}.".format(e))
    else:
        print("The information collection process was not activated. The existing local copy will be loaded instead.")


# Fetch a data frame with subregion information for each continent
def fetch_continents_subregion_tables(update=False):
    """
    :param update: [bool] whether to update (i.e. re-collect) all subregion tables for each continent
    :return: [pandas.DataFrame] or null
    """
    path_to_pickle = cd_dat("GeoFabrik-continents-subregion-tables.pickle")
    if not os.path.isfile(path_to_pickle) or update:
        collect_continents_subregion_tables(confirmation_required=True)
    try:
        subregion_tables = load_pickle(path_to_pickle)
        return subregion_tables
    except Exception as e:
        print(e)


# Scan through the downloading pages to collect a catalogue of region-subregion tier
def collect_region_subregion_tier(confirmation_required=True):
    """
    :param confirmation_required: [bool] whether to confirm before starting to collect region-subregion tier
    """

    # Find out the all regions and their subregions
    def compile_region_subregion_tier(sub_reg_tbls):
        """
        :param sub_reg_tbls: [pandas.DataFrame] obtained from fetch_continents_subregion_tables()
        :return: ([dict], [list]) a dictionary of region-subregion, and a list of (sub)regions without subregions
        """
        having_subregions = copy.deepcopy(sub_reg_tbls)
        region_subregion_tiers = copy.deepcopy(sub_reg_tbls)

        non_subregions_list = []
        for k, v in sub_reg_tbls.items():
            if v is not None and isinstance(v, pd.DataFrame):
                region_subregion_tiers = update_nested_dict(sub_reg_tbls, {k: set(v.Subregion)})
            else:
                non_subregions_list.append(k)

        for x in non_subregions_list:
            having_subregions.pop(x)

        having_subregions_temp = copy.deepcopy(having_subregions)

        while having_subregions_temp:

            for region_name, subregion_table in having_subregions.items():
                #
                subregion_names, subregion_links = subregion_table.Subregion, subregion_table.SubregionURL
                sub_subregion_tables = dict(
                    zip(subregion_names, [get_subregion_table(link) for link in subregion_links]))

                subregion_index, without_subregion_ = compile_region_subregion_tier(sub_subregion_tables)
                non_subregions_list += without_subregion_

                region_subregion_tiers.update({region_name: subregion_index})

                having_subregions_temp.pop(region_name)

        return region_subregion_tiers, non_subregions_list

    if confirmed("To compile a region-subregion tier? (Note that it may take a few minutes.) ",
                 confirmation_required=confirmation_required):
        try:
            subregion_tables = fetch_continents_subregion_tables(update=True)
            region_subregion_tier, non_subregions = compile_region_subregion_tier(subregion_tables)
            save_pickle(region_subregion_tier, cd_dat("GeoFabrik-region-subregion-tier.pickle"))
            save_json(region_subregion_tier, cd_dat("GeoFabrik-region-subregion-tier.json"))
            save_pickle(non_subregions, cd_dat("GeoFabrik-non-subregion-list.pickle"))
        except Exception as e:
            print("Failed to get the required information ... {}.".format(e))
    else:
        print("The information collection process was not activated. The existing local copy will be loaded instead.")


# Fetch a catalogue of region-subregion tier, or all regions having no subregions
def fetch_region_subregion_tier(catalogue_name, file_format=".pickle", update=False):
    """
    :param catalogue_name: [str] e.g. "GeoFabrik-region-subregion-tier"
    :param file_format: [str] ".pickle"(default), or ".json"
    :param update: [bool] whether to update (i.e. re-collect) all subregion tables for each continent
    :return: [dict, or list] or null
    """
    available_catalogue = ("GeoFabrik-region-subregion-tier", "GeoFabrik-non-subregion-list")
    assert catalogue_name in available_catalogue, \
        "'catalogue_name' must be one of the following: \n  \"{}\".".format("\",\n  \"".join(available_catalogue))

    available_fmt = (".pickle", ".json")
    assert file_format in available_fmt, \
        "'file_format' must be one of the following: \n  \"{}\".".format("\",\n  \"".join(available_fmt))

    path_to_file = cd_dat(catalogue_name + file_format)
    if not os.path.isfile(path_to_file) or update:
        collect_region_subregion_tier(confirmation_required=True)
    try:
        index_file = load_pickle(path_to_file) if file_format == ".pickle" else load_json(path_to_file)
        return index_file
    except Exception as e:
        print(e)


# Rectify the input subregion name in order to make it match the available subregion name
def regulate_input_subregion_name(subregion_name):
    """
    :param subregion_name: [str] subregion name, e.g. 'London'
    :return: [str] default subregion name that matches, or is the most similar to, the input 'subregion_name'
    """
    assert isinstance(subregion_name, str)
    # Get a list of available
    subregion_names = fetch_subregion_info_catalogue('GeoFabrik-subregion-name-list')
    subregion_name_, _ = fuzzywuzzy.process.extractOne(subregion_name, subregion_names, score_cutoff=50)
    return subregion_name_


# Get download URL
def get_subregion_download_url(subregion_name, osm_file_format, update=False):
    """
    :param subregion_name: [str] case-insensitive, e.g. 'Greater London'
    :param osm_file_format: [str] ".osm.pbf", ".shp.zip", or ".osm.bz2"
    :param update: [bool] whether to update subregion-downloads catalogue
    :return: [tuple] of length=2
    """
    available_fmt = [".osm.pbf", ".shp.zip", ".osm.bz2"]
    assert osm_file_format in available_fmt, "'file_format' must be one of {}.".format(available_fmt)

    # Get an index of download URLs
    subregion_downloads_index = fetch_subregion_info_catalogue("GeoFabrik-subregion-downloads-catalogue", update=update)
    subregion_downloads_index.set_index('Subregion', inplace=True)

    subregion_name_ = regulate_input_subregion_name(subregion_name)
    download_url = subregion_downloads_index.loc[subregion_name_, osm_file_format]  # Get the URL

    return subregion_name_, download_url


# Parse the download URL so as to get default filename for the given subregion name
def get_default_osm_filename(subregion_name, osm_file_format, update=False):
    """
    :param subregion_name: [str] case-insensitive, e.g. 'greater London', 'london'
    :param osm_file_format: [str] ".osm.pbf" (default), ".shp.zip", or ".osm.bz2"
    :param update: [bool] whether to update source data
    :return: [str] default OSM filename of the 'subregion_name'
    """
    _, download_url = get_subregion_download_url(subregion_name, osm_file_format, update=update)
    subregion_filename = os.path.split(download_url)[-1]
    return subregion_filename


# Parse the download URL so as to specify a path for storing the downloaded file
def get_default_path_to_osm_file(subregion_name, osm_file_format, mkdir=False, update=False):
    """
    :param subregion_name: [str] case-insensitive, e.g. 'greater London', 'london'
    :param osm_file_format: [str] ".osm.pbf" (default), ".shp.zip", or ".osm.bz2"
    :param mkdir: [bool]  whether to create a directory
    :param update: [bool] whether to update source data
    :return: [tuple] (of length 2), including filename of the subregion, and path to the file
    """
    _, download_url = get_subregion_download_url(subregion_name, osm_file_format, update=update)
    parsed_path = urllib.parse.urlparse(download_url).path.lstrip('/').split('/')

    subregion_names = fetch_subregion_info_catalogue("GeoFabrik-subregion-name-list")
    directory = cd_dat_geofabrik(*[fuzzywuzzy.process.extractOne(x, subregion_names)[0] for x in parsed_path[0:-1]])

    default_filename = parsed_path[-1]

    if not os.path.exists(directory) and mkdir:
        os.makedirs(directory)
    default_file_path = os.path.join(directory, default_filename)

    return default_filename, default_file_path


# Retrieve names of all subregions (if available) from the catalogue of region-subregion tier
def retrieve_subregion_names_from(*subregion_name):
    """
    Reference:
    https://stackoverflow.com/questions/9807634/find-all-occurrences-of-a-key-in-nested-python-dictionaries-and-lists
    :param subregion_name: [str] case-insensitive, e.g. 'greater London', 'london'
    :return: [str or None] (list of) subregions if available; None otherwise
    """

    def find_subregions(reg_name, reg_sub_idx):
        for k, v in reg_sub_idx.items():
            if reg_name == k:
                if isinstance(v, dict):
                    yield list(v.keys())
                else:
                    yield [reg_name] if isinstance(reg_name, str) else reg_name
            elif isinstance(v, dict):
                for sub in find_subregions(reg_name, v):
                    if isinstance(sub, dict):
                        yield list(sub.keys())
                    else:
                        yield [sub] if isinstance(sub, str) else sub

    no_subregion_list = fetch_region_subregion_tier("GeoFabrik-non-subregion-list")
    if not subregion_name:
        result = no_subregion_list
    else:
        region_subregion_index = fetch_region_subregion_tier("GeoFabrik-region-subregion-tier")
        res = []
        for region in subregion_name:
            res += list(find_subregions(regulate_input_subregion_name(region), region_subregion_index))[0]
        check_list = [x for x in res if x not in no_subregion_list]
        if check_list:
            result = list(set(res) - set(check_list))
            for region in check_list:
                result += retrieve_subregion_names_from(region)
        else:
            result = res
        del no_subregion_list, region_subregion_index, check_list
    return list(dict.fromkeys(result))


# Download OSM data files
def download_subregion_osm_file(*subregion_name, osm_file_format, download_dir=None, update=False,
                                download_confirmation_required=True, verbose=True):
    """
    :param subregion_name: [str] case-insensitive, e.g. 'greater London', 'london'
    :param osm_file_format: [str] ".osm.pbf", ".shp.zip", or ".osm.bz2"
    :param download_dir: [str] directory to save the downloaded file(s), or None (using default directory)
    :param update: [bool] whether to update (i.e. re-download) data
    :param download_confirmation_required: [bool] whether to confirm before downloading
    :param verbose: [bool]
    """
    for sub_reg_name in subregion_name:

        # Get download URL
        subregion_name_, download_url = get_subregion_download_url(sub_reg_name, osm_file_format, update=False)

        if not download_dir:
            # Download the requested OSM file to default directory
            osm_filename, path_to_file = get_default_path_to_osm_file(subregion_name_, osm_file_format, mkdir=True)
        else:
            regulated_dir = regulate_input_data_dir(download_dir)
            osm_filename = get_default_osm_filename(subregion_name_, osm_file_format=osm_file_format)
            path_to_file = os.path.join(regulated_dir, osm_filename)

        if os.path.isfile(path_to_file) and not update:
            if verbose:
                print("\n\"{}\" is already available for \"{}\" at: \n\"{}\".\n".format(
                    osm_filename, subregion_name_, path_to_file))
            else:
                pass
        else:
            if confirmed("\nTo download {} data for {}".format(osm_file_format, subregion_name_),
                         confirmation_required=download_confirmation_required):

                op = "Updating" if os.path.isfile(path_to_file) else "Downloading"
                try:
                    download(download_url, path_to_file)
                    print("\n{} \"{}\" for \"{}\" ... Done.".format(op, osm_filename, subregion_name_))
                    print("Check out: \"{}\".".format(path_to_file))
                except Exception as e:
                    print("\nFailed to download \"{}\". {}.".format(osm_filename, e))
            else:
                print("The downloading process was not activated.")


# Make OSM data available for a given region and (optional) all subregions of it
def download_sub_subregion_osm_file(*subregion_name, osm_file_format, download_dir=None, update=False,
                                    download_confirmation_required=True, interval_sec=5):
    """
    :param subregion_name: [str] case-insensitive, e.g. 'greater London', 'london'
    :param osm_file_format: [str] ".osm.pbf", ".shp.zip", or ".osm.bz2"
    :param download_dir: [str or None] directory to save the downloaded file(s), or None (using default directory)
    :param update: [bool] whether to update (i.e. re-download) data
    :param download_confirmation_required: [bool] whether to confirm before downloading
    :param interval_sec: [int or None] interval (in sec) between downloading two subregions
    """
    subregions = retrieve_subregion_names_from(*subregion_name)
    if confirmed("\nTo download {} data for all the following subregions: \n{}?\n".format(
            osm_file_format, ", ".join(subregions)), confirmation_required=download_confirmation_required):
        download_subregion_osm_file(*subregions, osm_file_format=osm_file_format, download_dir=download_dir,
                                    update=update, download_confirmation_required=False)
        if interval_sec:
            time.sleep(interval_sec)


# Remove the downloaded file
def remove_subregion_osm_file(path_to_osm_file):
    """
    :param path_to_osm_file: [str] 
    """
    assert any(path_to_osm_file.endswith(ext) for ext in [".osm.pbf", ".shp.zip", ".osm.bz2"]), \
        "'subregion_file_path' is not valid."
    if os.path.isfile(path_to_osm_file):
        try:
            os.remove(path_to_osm_file)
            print("\"{}\" has been removed from local disk.\n".format(os.path.basename(path_to_osm_file)))
        except Exception as e:
            print(e)
            pass
    else:
        print("\"{}\" does not exist at \"{}\".\n".format(*os.path.split(path_to_osm_file)[::-1]))
