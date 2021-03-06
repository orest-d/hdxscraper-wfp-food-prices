#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
WFP food prices:
------------

Creates datasets with flattened tables of WFP food prices.

"""
import difflib
import logging
import re

from hdx.data.dataset import Dataset
from hdx.data.showcase import Showcase
from hdx.location.country import Country
from hdx.utilities.dictandlist import dict_of_lists_add
from hdx.utilities.downloader import DownloadError
from slugify import slugify

logger = logging.getLogger(__name__)

hxltags = {'date': '#date', 'adm1name': '#adm1+name', 'adm2name': '#adm2+name', 'latitude': '#geo+lat',
           'longitude': '#geo+lon', 'market': '#name+market', 'category': '#item+type', 'commodity': '#item+name',
           'unit': '#item+unit', 'currency': '#currency', 'pricetype': '#item+price+type', 'price': '#value'}
headers = list(hxltags.keys())
qc_hxltags = {'date': '#date', 'code': '#meta+code', 'price': '#value'}


class WFPFood:
    def __init__(self, configuration, token_downloader, retriever):
        self.configuration = configuration
        self.token_downloader = token_downloader
        self.retriever = retriever
        self.headers = None
        self.commodity_to_category = dict()

    def refresh_headers(self):
        self.token_downloader.download(self.configuration['token_url'], post=True,
                                       parameters={'grant_type': 'client_credentials'})
        access_token = self.token_downloader.get_json()['access_token']
        self.headers = {'Accept': 'application/json', 'Authorization': f'Bearer {access_token}'}

    def retrieve(self, url, filename, log, parameters=None):
        try:
            results = self.retriever.retrieve_json(url, filename, log, False, parameters=parameters,
                                                   headers=self.headers)
        except DownloadError:
            if self.retriever.downloader.response.status_code != 401:
                raise
            self.refresh_headers()
            results = self.retriever.retrieve_json(url, filename, log, False, parameters=parameters,
                                                   headers=self.headers)
        return results

    def get_countries(self):
        url = self.configuration['countries_url']
        json = self.retrieve(url, 'countries.json', 'countries')
        countries = set()
        for country in json['response']:
            countries.add((country['iso3'], country['adm0_name']))
        return [{'iso3': x[0], 'name': x[1]} for x in sorted(countries)]

    def get_list(self, endpoint, countryiso3=None, startdate=None):
        url = f'{self.configuration["base_url"]}{endpoint}'
        base_filename = url.split('/')[-2]
        page = 1
        all_data = []
        data = None
        while data is None or len(data) > 0:
            parameters = {'page': page}
            if countryiso3 is None:
                filename = f'{base_filename}_{page}.json'
                log = f'{base_filename} page {page}'
            else:
                filename = f'{base_filename}_{countryiso3}_{page}.json'
                log = f'{base_filename} for {countryiso3} page {page}'
                parameters['CountryCode'] = countryiso3
            if startdate:
                parameters['startDate'] = startdate
            try:
                json = self.retrieve(url, filename, log, parameters)
            except FileNotFoundError:
                json = {'items': list()}
            data = json['items']
            all_data.extend(data)
            page = page + 1
        return all_data

    def build_mappings(self):
        categoryid_to_name = dict()
        for category in self.get_list('Commodities/Categories/List'):
            categoryid_to_name[category['id']] = category['name']
        for commodity in self.get_list('Commodities/List'):
            self.commodity_to_category[commodity['id']] = categoryid_to_name[commodity['categoryId']]

    @staticmethod
    def match_source(sources, source):
        words = source.split(' ')
        if len(words) < 2:
            return False
        found = False
        for cursource in sources:
            words = cursource.split(' ')
            if len(words) < 2:
                continue
            seq = difflib.SequenceMatcher(None, source, cursource)
            if seq.ratio() > 0.9:
                found = True
        return found

    def generate_dataset_and_showcase(self, countryiso3, folder):
        countryname = Country.get_country_name_from_iso3(countryiso3)
        title = f'{countryname} - Food Prices'
        logger.info(f'Creating dataset: {title}')
        name = f'WFP food prices for {countryname}'
        slugified_name = slugify(name).lower()

        dataset = Dataset({
            'name': slugified_name,
            'title': title,
        })
        dataset.set_maintainer('f1921552-8c3e-47e9-9804-579b14a83ee3')
        dataset.set_organization('3ecac442-7fed-448d-8f78-b385ef6f84e7')

        dataset.set_expected_update_frequency('weekly')
        dataset.add_country_location(countryname)
        dataset.set_subnational(True)
        tags = ['commodities', 'prices', 'markets', 'hxl']
        dataset.add_tags(tags)

        prices_data = self.get_list('MarketPrices/PriceMonthly', countryiso3)
        if not prices_data:
            logger.info(f'{countryiso3} has no prices data!')
            return None, None, None
        market_to_adm = dict()
        for market in self.get_list('Markets/List', countryiso3):
            market_to_adm[market['marketId']] = market['admin1Name'], market['admin2Name'], market['marketLatitude'],\
                                                market['marketLongitude']

        rows = dict()
        sources = dict()
        markets = dict()
        for price_data in prices_data:
            if price_data['commodityPriceFlag'] not in ('actual', 'aggregate'):
                continue
            date = price_data['commodityPriceDate']
            category = self.commodity_to_category[price_data['commodityID']]
            market = price_data['marketName']
            if market == 'National Average':
                adm1 = adm2 = lat = lon = ''
            else:
                market_id = price_data['marketID']
                if market_id in market_to_adm:
                    adm1, adm2, lat, lon = market_to_adm[market_id]
                else:
                    adm1 = adm2 = lat = lon = ''
            orig_source = price_data['commodityPriceSourceName'].replace('M/o', 'Ministry of').replace('+', '/')
            regex = r'Government.*,(Ministry.*)'
            match = re.search(regex, orig_source)
            if match:
                split_sources = [match.group(1)]
            else:
                split_sources = orig_source.replace(',', '/').replace(';', '/').split('/')
            for source in split_sources:
                source = source.strip()
                if not source:
                    continue
                if source[-1] == '.':
                    source = source[:-1]
                source_lower = source.lower()
                if 'mvam' in source_lower and len(source_lower) <= 8:
                    source = 'WFP mVAM'
                elif '?stica' in source:
                    source = source.replace('?stica', 'ística')
                source_lower = source.lower()
                if not self.match_source(sources.keys(), source_lower):
                    sources[source_lower] = source
            commodity = price_data['commodityName']
            unit = price_data['commodityUnitName']
            price = price_data['commodityPrice']
            currency = price_data['currencyName']
            pricetype = price_data['commodityPriceFlag']
            key = date, adm1, adm2, market, category, commodity, unit
            rows[key] = {'date': date, 'adm1name': adm1, 'adm2name': adm2, 'market': market, 'latitude': lat,
                         'longitude': lon, 'category': category, 'commodity': commodity, 'unit': unit,
                         'currency': currency, 'pricetype': pricetype, 'price': price}
            if adm1 and adm2 and category:
                adm1adm2market = adm1, adm2, market
                commodities = markets.get(adm1adm2market, dict())
                dict_of_lists_add(commodities, (commodity, unit, currency), (date, price))
                markets[adm1adm2market] = commodities
        if not rows:
            logger.info(f'{countryiso3} has no prices!')
            return None, None, None
        number_market = list()
        for key, commodities in markets.items():
            number_market.append((len(commodities), key))
        number_market = sorted(number_market, reverse=True)
        qc_indicators = list()
        qc_rows = [qc_hxltags]
        chosen_commodities = set()
        # Go through markets starting with the one with most commodities
        for _, adm1adm2market in number_market:
            commodities = markets[adm1adm2market]
            number_commodity = list()
            for commodityunitcurrency, details in commodities.items():
                number_commodity.append((len(details), commodityunitcurrency))
            number_commodity = sorted(number_commodity, reverse=True)
            index = 0
            # Pick commodity with most rows that has not already been used for another market
            commodity, unit, currency = number_commodity[index][1]
            while commodity in chosen_commodities:
                index += 1
                if index == len(number_commodity):
                    commodity, unit, currency = number_commodity[0][1]
                    break
                commodity, unit, currency = number_commodity[index][1]
            adm1, adm2, market = adm1adm2market
            code = f'{adm1}-{adm2}-{market}-{commodity}-{unit}-{currency}'
            for date, price in sorted(commodities[(commodity, unit, currency)]):
                qc_rows.append({'date': date, 'code': code, 'price': price})
            chosen_commodities.add(commodity)
            marketname = market
            if adm2 != market:
                marketname = f'{adm2}/{marketname}'
            if adm1 != adm2:
                marketname = f'{adm1}/{marketname}'
            qc_indicators.append({'code': code, 'title': f'Price of {commodity} in {market}',
                                  'unit': f'Currency {currency}',
                                  'description': f'Price of {commodity} ({currency}/{unit}) in {marketname}',
                                  'code_col': '#meta+code', 'value_col': '#value', 'date_col': '#date'})
            if len(qc_indicators) == 3:
                break
        dataset['dataset_source'] = ', '.join(sorted(sources.values()))
        filename = f'wfp_food_prices_{countryiso3.lower()}.csv'
        resourcedata = {
            'name': title,
            'description': 'Food prices data with HXL tags',
            'format': 'csv'
        }
        rows = [rows[key] for key in sorted(rows)]
        dataset.generate_resource_from_iterator(headers, rows, hxltags, folder, filename, resourcedata, datecol='date')
        filename = f'wfp_food_prices_{countryiso3.lower()}_qc.csv'
        resourcedata = {
            'name': f'QuickCharts: {title}',
            'description': 'Food prices QuickCharts data with HXL tags',
            'format': 'csv'
        }
        dataset.generate_resource_from_rows(folder, filename, qc_rows, resourcedata, headers=list(qc_hxltags.keys()))
        showcase = Showcase({
            'name': f'{slugified_name}-showcase',
            'title': f'{title} showcase',
            'notes': f'{countryname} food prices data from World Food Programme displayed through VAM Economic Explorer',
            'url': f'http://dataviz.vam.wfp.org/economic_explorer/prices?iso3={countryiso3}',
            'image_url': 'http://dataviz.vam.wfp.org/_images/home/3_economic.jpg'
        })
        showcase.add_tags(tags)
        return dataset, showcase, qc_indicators