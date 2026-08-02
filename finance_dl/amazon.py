"""Retrieves order invoices from Amazon.

This uses the `selenium` Python package in conjunction with `chromedriver` to
scrape the Amazon website.

Configuration:
==============

The following keys may be specified as part of the configuration dict:

- `credentials`: Required.  Must be a `dict` with `'username'` and `'password'`
  keys.

- `output_directory`: Required.  Must be a `str` that specifies the path on the
  local filesystem where the output will be written.  If the directory does not
  exist, it will be created.

- `dir_per_year`: Optional. If true (default is false), adds one subdirectory
  to the output for each year's worth of transactions. Useful for filesystems
  that struggle with very large directories. Probably not that useful for
  actually finding anything, given the uselessness of Amazon's order ID
  scheme.

- `amazon_domain`: Optional.  Specifies the Amazon domain from which to download
  orders.  Must be one of `'.com'`, `'.co.cuk'` or `'.de'`.  Defaults to
  `'.com'`.

- `regular`: Optional.  Must be a `bool`.  If `True` (the default), download regular orders.
   For domains other than `amazon_domain=".com"`, `True` downloads regular AND digital orders.

- `digital`: Optional.  Must be a `bool` or `None`.  If `True`, download digital
  orders. Effective only for `amazon_domain=".com"`. Defaults to `True` for
  `amazon_domain=".com"`. For other domains, digital invoices are downloaded
  tgehter with regular invoices since there is no separate menu on the amazon website.

- `profile_dir`: Optional.  If specified, must be a `str` that specifies the
  path to a persistent Chrome browser profile to use.  This should be a path
  used solely for this single configuration; it should not refer to your normal
  browser profile.  If not specified, a fresh temporary profile will be used
  each time.

- `order_groups`: Optional.  If specified, must be a list of strings specifying the Amazon
  order page "order groups" that will be scanned for orders to download. Order groups
  include years (e.g. '2020'), as well as 'last 30 days' and 'past 3 months'.

- `download_preorder_invoices`: Optional. If specified and True, invoices for
  preorders (i.e. orders that have not actually been charged yet) will be
  skipped. Such preorder invoices are not typically useful for accounting
  since they claim a card was charged even though it actually has not been
  yet; they get replaced with invoices containing the correct information when
  the order is actually fulfilled.

- `monthly_invoices`: Optional. Must be a `bool`. If `True`, also download
  Amazon's consolidated "My Invoices" / "Monatsabrechnung" statements from
  `/cpe/myinvoices`. Each month's statement is saved as an HTML page (the
  same content Amazon's "Monatsabrechnung drucken" button prints) into a
  `monthly/` subdirectory of `output_directory`, named
  `<statementId>.html`. Defaults to `False`. Combine with `regular=False,
  digital=False` to download monthly statements only.

- `gift_cards`: Optional. Must be a `bool`. If `True`, also download the gift
  card balance / activity history from `/gc/balance`, following the `next=`
  pager to the end. Pages are saved as `giftcard/page-NNN.html` under
  `output_directory`. Defaults to `False`. Combine with `regular=False,
  digital=False` to download the gift card history only.

  Worth downloading even if you already fetch orders: order history shows only
  the debits ("Gift Card applied to order"). The credits — claim codes
  redeemed, refunds on gift-card-paid orders returning to the balance rather
  than to a card, and "Release of Gift Card Balance" — appear nowhere else.

Output format:
==============

Each regular or digital order invoice is written in HTML format to the specified
`output_directory` using the naming scheme `<order-id>.html`,
e.g. `166-7926740-5141621.html` for a regular order invoice and
`D56-5204779-4181560.html` for a digital order invoice.

Example:
========

    def CONFIG_amazon():
        return dict(
            module='finance_dl.amazon',
            credentials={
                'username': 'XXXXXX',
                'password': 'XXXXXX',
            },
            output_directory=os.path.join(data_dir, 'amazon'),
            # profile_dir is optional.
            profile_dir=os.path.join(profile_dir, 'amazon'),
            # order_groups is optional.
            order_groups=['past 3 months'],
        )

Interactive shell:
==================

From the interactive shell, type: `self.run()` to start the scraper.

"""
import dataclasses
import urllib.parse
import re
import logging
import os
import datetime
import dateutil.parser
import bs4
from selenium.webdriver.common.by import By
from selenium.webdriver.common.virtual_authenticator import VirtualAuthenticatorOptions
from selenium.webdriver.support.ui import Select
from selenium.webdriver.common.keys import Keys
from atomicwrites import atomic_write
from . import scrape_lib
from typing import List, Optional

logger = logging.getLogger('amazon_scrape')


@dataclasses.dataclass
class Domain():
    top_level: str

    sign_in: str
    sign_out: str

    # Find invoices.
    your_orders: str
    archived_orders: str
    invoice: str
    invoice_link: List[str]
    order_summary: str
    order_summary_hidden: bool
    next: str

    # Confirm invoice page
    grand_total: str
    grand_total_digital: str
    order_cancelled: str
    pre_order: str

    digital_order: str
    regular_order_placed: str

    # .COM: digital orders have own order list
    # other domains: digital orders are in the regular order list
    digital_orders_menu: bool
    digital_orders_menu_text: Optional[str] = None

    fresh_fallback: Optional[str] = None

    # Path of the "My Invoices" page (Amazon's consolidated invoice library:
    # Prime, subscriptions, business / VAT invoices, etc.). Set to None to
    # disable monthly invoice downloads on this domain.
    monthly_invoices_path: Optional[str] = '/cpe/myinvoices'

    # Path of the gift card balance / activity page. Set to None to disable
    # gift card downloads on this domain.
    gift_card_path: Optional[str] = '/gc/balance'


class DOT_COM(Domain):
    def __init__(self) -> None:
        super().__init__(
            top_level='com',
            sign_in='Sign In',
            sign_out='Sign Out',

            your_orders='Your Orders',
            archived_orders='Archived Orders',
            invoice='Invoice',
            invoice_link=["View order", "View invoice"],
            # View invoice -> regular/digital order, View order -> Amazon Fresh
            fresh_fallback="View order",
            order_summary='Order Summary',
            order_summary_hidden=False,
            next='Next',

            grand_total='Grand Total:',
            grand_total_digital='Grand Total:',
            order_cancelled='Order Canceled',
            pre_order='Pre-order',

            digital_order='Digital Order: (.*)',
            regular_order_placed=r'(?:Subscribe and Save )?Order Placed:\s+([^\s]+ \d+, \d{4})',

            digital_orders_menu=True,
            digital_orders_menu_text='Digital Orders',
            )

    @staticmethod
    def parse_date(date_str) -> datetime.date:
        return dateutil.parser.parse(date_str).date()

class DOT_CO_UK(Domain):
    def __init__(self) -> None:
        super().__init__(
            top_level='co.uk',
            sign_in='Sign in',
            sign_out='Sign out',

            your_orders='Your Orders',
            archived_orders='Archived Orders',
            invoice='Invoice',
            invoice_link=["View order", "View invoice"],
            # View invoice -> regular/digital order, View order -> Amazon Fresh
            fresh_fallback="View order",
            order_summary='Order Summary',
            order_summary_hidden=False,
            next='Next',

            grand_total='Grand Total:',
            grand_total_digital='Grand Total:',
            order_cancelled='Order Canceled',
            pre_order='Pre-order',

            digital_order='Digital Order: (.*)',
            regular_order_placed=r'(?:Subscribe and Save )?Order Placed:\s+([^\s]+ \d+, \d{4})',

            digital_orders_menu=False,
            )

    @staticmethod
    def parse_date(date_str) -> datetime.date:
        return dateutil.parser.parse(date_str).date()

class DOT_DE(Domain):
    def __init__(self) -> None:
        super().__init__(
            top_level='de',
            sign_in='Anmelden',
            sign_out='Abmelden',

            your_orders='Meine Bestellungen',
            archived_orders='Archivierte Bestellungen',
            invoice='Rechnung',
            invoice_link=["Bestelldetails anzeigen"],
            fresh_fallback=None,
            order_summary='Bestellübersicht',
            order_summary_hidden=False,
            next='Weiter',

            grand_total='Gesamtsumme:',
            grand_total_digital='Endsumme:',
            order_cancelled='Order Canceled',
            pre_order='Pre-order',

            digital_order='Digitale Bestellung: (.*)',
            regular_order_placed=r'(?:Getätigte Spar-Abo-Bestellung|Bestellung aufgegeben am):\s+(\d+\. [^\s]+ \d{4})',

            digital_orders_menu=False,
            )

    class _parserinfo(dateutil.parser.parserinfo):
        MONTHS=[
            ('Jan', 'Januar'), ('Feb', 'Februar'), ('Mär', 'März'),
            ('Apr', 'April'), ('Mai', 'Mai'), ('Jun', 'Juni'),
            ('Jul', 'Juli'), ('Aug', 'August'), ('Sep', 'September'),
            ('Okt', 'Oktober'), ('Nov', 'November'), ('Dez', 'Dezember')
            ]
    
    @staticmethod
    def parse_date(date_str) -> datetime.date:
        return dateutil.parser.parse(date_str, parserinfo=DOT_DE._parserinfo(dayfirst=True)).date()

DOMAINS = {
    ".com": DOT_COM,
    ".co.uk": DOT_CO_UK, 
    ".de": DOT_DE
    }


class Scraper(scrape_lib.Scraper):
    def __init__(self,
                 credentials,
                 output_directory,
                 dir_per_year=False,
                 amazon_domain: str = ".com",
                 regular: bool = True,
                 digital: Optional[bool] = None,
                 order_groups: Optional[List[str]] = None,
                 download_preorder_invoices: bool = False,
                 monthly_invoices: bool = False,
                 gift_cards: bool = False,
                 **kwargs):
        super().__init__(**kwargs)
        self.driver.add_virtual_authenticator(VirtualAuthenticatorOptions())
        if amazon_domain not in DOMAINS:
          raise ValueError(f"Domain '{amazon_domain} not supported. Supported "
                           f"domains: {list(DOMAINS)}")
        self.domain = DOMAINS[amazon_domain]()
        self.credentials = credentials
        self.output_directory = output_directory
        self.dir_per_year = dir_per_year
        self.logged_in = False
        self.regular = regular
        self.digital_orders_menu = digital if digital is not None else self.domain.digital_orders_menu
        self.order_groups = order_groups
        self.download_preorder_invoices = download_preorder_invoices
        self.monthly_invoices = monthly_invoices
        self.gift_cards = gift_cards

    def check_url(self, url):
        netloc_re = r'^([^\.@]+\.)*amazon.' + self.domain.top_level + '$'
        result = urllib.parse.urlparse(url)
        if result.scheme != 'https' or not re.fullmatch(netloc_re, result.netloc):
            raise RuntimeError('Reached invalid URL: %r' % url)

    def check_after_wait(self):
        self.check_url(self.driver.current_url)

    def dismiss_cookie_banner(self):
        """Dismiss the cookie consent banner if present."""
        try:
            reject_buttons = self.find_visible_elements(
                By.ID, 'sp-cc-rejectall-link')
            if reject_buttons:
                logger.info('Dismissing cookie consent banner')
                self.click(reject_buttons[0])
        except Exception:
            pass

    def login(self):
        logger.info('Initiating log in')
        self.driver.get('https://www.amazon.' + self.domain.top_level)
        if self.logged_in:
            return

        self.dismiss_cookie_banner()

        sign_out_links = self.find_elements_by_descendant_partial_text(self.domain.sign_out, 'a')
        if len(sign_out_links) > 0:
            logger.info('You must be already logged in!')
            self.logged_in = True
            return

        logger.info('Looking for sign-in link')
        sign_in_links, = self.wait_and_return(
            lambda: self.find_visible_elements_by_descendant_partial_text(self.domain.sign_in, 'a')
        )

        self.click(sign_in_links[0])
        logger.info('Looking for username link')
        (username, ), = self.wait_and_return(
            lambda: self.find_visible_elements(By.XPATH, '//input[@type="email"]')
        )
        username.send_keys(self.credentials['username'])
        username.send_keys(Keys.ENTER)

        self.finish_login()

    def finish_login(self):
        logger.info('Looking for password link')
        (password, ), = self.wait_and_return(
            lambda: self.find_visible_elements(By.XPATH, '//input[@type="password"]')
        )
        password.send_keys(self.credentials['password'])

        remember_me_elements = self.find_visible_elements(
            By.XPATH, '//input[@name="rememberMe"]')
        if remember_me_elements:
            logger.info('Clicking "remember me" checkbox')
            remember_me_elements[0].click()

        with self.wait_for_page_load():
            password.send_keys(Keys.ENTER)

        logger.info('Logged in')
        self.logged_in = True

    def get_invoice_path(self, year, order_id):
        if self.dir_per_year:
            return os.path.join(self.output_directory, str(year), order_id + '.html')
        return os.path.join(self.output_directory, order_id + '.html')

    def get_order_id(self, href) -> str:
        m = re.match('.*[&?]orderI[Dd]=((?:D)?[0-9\\-]+)(?:&.*)?$', href)
        if m is None:
            raise RuntimeError(
                'Failed to parse order ID from href %r' % (href, ))
        return m[1]

    def get_orders(self, regular=True, digital_orders_menu=True):
        invoice_hrefs = []
        order_ids_seen = set()
        order_ids_downloaded = frozenset([
            name[:len(name)-5]
            for _, _, files in os.walk(self.output_directory)
            for name in files
            if name.endswith('.html')
        ])

        def get_invoice_urls():
            initial_iteration = True
            while True:
                # break when there is no "next page"

                # Problem: different site structures depending on country
                
                # .com / .uk
                # Order Summary buttons are directly visible and can be
                # identified with href containing "orderID="
                # but order summary may have different names, e.g. for Amazon Fresh orders
                
                # .de
                # only link with href containing "orderID=" is "Bestelldetails anzeigen" (=Order Details)
                # which is not helpful
                # order summary is hidden behind submenu which requires a click to be visible

                def invoice_finder():
                    # order summary link is visible on page
                    elements_raw = self.driver.find_elements(
                        By.XPATH, '//a[contains(@href, "orderID=")]')
                    elements = []
                    for invoice_link in elements_raw:
                        if invoice_link.text not in self.domain.invoice_link:
                            # skip invoice if label is not known
                            # different labels are possible e.g. for regular orders vs. Amazon fresh
                            if invoice_link.text != "":
                                # log non-empty link texts -> may be new type
                                logger.debug(
                                    'Skipping invoice due to unknown invoice_link.text: %s',
                                    invoice_link.text)
                        else:
                            elements.append(invoice_link)
                    return elements

                if initial_iteration:
                    invoices = invoice_finder()
                else:
                    invoices, = self.wait_and_return(invoice_finder)
                initial_iteration = False

                def invoice_link_finder(invoice_link):
                    href = invoice_link.get_attribute('href')
                    order_id = self.get_order_id(href)
                    if self.domain.fresh_fallback is not None and invoice_link.text == self.domain.fresh_fallback:
                        # Amazon Fresh order, construct link to invoice
                        logger.info("   Found likely Amazon Fresh order. Falling back to direct invoice URL.")
                        tokens = href.split("/")
                        tokens = tokens[:4]
                        tokens[-1] = f"gp/css/summary/print.html?orderID={order_id}"
                        href = "/".join(tokens)
                    return (order_id, href)
                
                def invoice_link_finder_hidden(invoice_link):
                    # get order id to later find the correct summary link
                    order_id=self.get_order_id(invoice_link.get_attribute('href'))
                    
                    # get parent element to search for invoice menu button (has no orderID specified)
                    parent=invoice_link.find_element(By.XPATH,"./..")
                    # leading dot in './/' specifies to only search in children
                    popover=parent.find_elements(By.XPATH,'.//a[contains(@href, "invoice/invoice.html")]')
                    # depending on the order group the XPATH may be different
                    if len(popover) == 0:
                        popover=parent.find_elements(
                            By.XPATH,
                            f'.//a[contains(text(), {self.domain.invoice}) and @class="a-popover-trigger a-declarative"]')

                    # open invoice popover to extract invoice link
                    popover[0].click()

                    # submenu containing order summary takes some time to load after click
                    summary_link, = self.wait_and_locate(
                        (By.XPATH,'//a[contains(@href,"{}") and contains(text(),"{}")]'.format(order_id, self.domain.order_summary)))
                    if summary_link:
                        href = summary_link.get_attribute('href')
                        return (order_id, href)
                    else:
                        logger.info('Link extraction failed for order id: %r', order_id)
                        return (False, False)

                for invoice_link in invoices:
                    if not self.domain.order_summary_hidden:
                        (order_id, href) = invoice_link_finder(invoice_link)
                    else:
                        (order_id, href) = invoice_link_finder_hidden(invoice_link)
                    if order_id:
                        if order_id in order_ids_seen:
                            logger.info('Skipping already-seen order id: %r', order_id)
                            continue
                        if order_id in order_ids_downloaded:
                            logger.info('Skipping already-downloaded invoice: %r', order_id)
                            continue
                        logger.info('Found order \'{}\''.format(order_id))
                        invoice_hrefs.append((href, order_id))
                        order_ids_seen.add(order_id)

                # Find next link
                next_links = self.find_elements_by_descendant_text_match(
                    f'. = "{self.domain.next}"', 'a', only_displayed=True)
                if len(next_links) == 0:
                    logger.info('Found no more pages')
                    break
                if len(next_links) != 1:
                    raise RuntimeError('More than one next link found')
                with self.wait_for_page_load():
                    logging.info("Next page.")
                    self.click(next_links[0])

        def retrieve_all_order_groups():
            order_select_index = 0

            while True:
                (order_filter,), = self.wait_and_return(
                    lambda: self.find_visible_elements(By.XPATH, '//select[@name="timeFilter"]')
                )
                order_select = Select(order_filter)
                num_options = len(order_select.options)
                if order_select_index >= num_options:
                    break
                option = order_select.options[
                    order_select_index]
                option_text = option.text.strip()
                order_select_index += 1
                if option_text == self.domain.archived_orders:
                    continue
                if self.order_groups is not None and option_text not in self.order_groups:
                    logger.info('Skipping order group: %r', option_text)
                    continue
                logger.info('Retrieving order group: %r', option_text)
                if not option.is_selected():
                    with self.wait_for_page_load():
                        order_select.select_by_index(order_select_index - 1)
                get_invoice_urls()

        if regular:
            # on co.uk, orders link is hidden behind the menu, hence not directly clickable
            (orders_link,), = self.wait_and_return(
                lambda: self.find_elements_by_descendant_text_match(f'. = "{self.domain.your_orders}"', 'a', only_displayed=False)
            )
            link = orders_link.get_attribute('href')
            scrape_lib.retry(lambda: self.driver.get(link), retry_delay=2)

            retrieve_all_order_groups()

        if digital_orders_menu:
            # orders in separate Digital Orders list (relevant for .COM)
            # other domains list digital orders within the regular order list
            (digital_orders_link,), = self.wait_and_return(
                lambda: self.find_elements_by_descendant_text_match(
                    f'contains(., "{self.domain.digital_orders_menu_text}")', 'a', only_displayed=True)
            )
            scrape_lib.retry(lambda: self.click(digital_orders_link),
                             retry_delay=2)
            retrieve_all_order_groups()

        self.retrieve_invoices(invoice_hrefs)

    def retrieve_invoices(self, invoice_hrefs):
        for href, order_id in invoice_hrefs:
            logger.info('Downloading invoice for order %r', order_id)
            with self.wait_for_page_load():
                self.driver.get(href)

            # For digital orders, Amazon dynamically generates some of the information.
            # Wait until it is all generated.
            def get_source():
                source = self.driver.page_source
                if (
                    self.domain.grand_total in source or
                    self.domain.grand_total_digital in source or
                    self.domain.order_cancelled in source or
                    order_id in source
                ):
                    return source
                elif 'problem loading this order' in source:
                    raise ValueError(f'Failed to retrieve information for order {order_id}')
                elif self.find_visible_elements(By.XPATH, '//input[@type="password"]'):
                    self.finish_login() # fallthrough

                return None

            page_source, = self.wait_and_return(get_source)
            if self.domain.pre_order in page_source and not self.download_preorder_invoices:
                    # Pre-orders don't have enough information to download yet. Skip them.
                    logger.info(f'Skipping pre-order invoice {order_id}')
                    return
            if order_id not in page_source:
                raise ValueError(f'Failed to retrieve information for order {order_id}')

            # extract order date
            def get_date(source, order_id):
                # code blocks taken from beancount-import/amazon-invoice.py
                soup=bs4.BeautifulSoup(source, 'lxml')

                def is_order_placed_node(node):
                    # order placed information in page header (top left)
                    m = re.fullmatch(self.domain.regular_order_placed, node.text.strip())
                    return m is not None
                
                def is_digital_order_row(node):
                    # information in heading of order table
                    if node.name != 'tr':
                        return False
                    m = re.match(self.domain.digital_order, node.text.strip())
                    if m is None:
                        return False
                    try:
                        self.domain.parse_date(m.group(1))
                        return True
                    except:
                        return False

                if order_id.startswith('D'):
                    # digital order
                    node = soup.find(is_digital_order_row)
                    regex = self.domain.digital_order
                else:
                    # regular order
                    node = soup.find(is_order_placed_node)
                    regex = self.domain.regular_order_placed
                
                if node is not None:
                    m = re.fullmatch(regex, node.text.strip())
                    if m is not None:
                        return self.domain.parse_date(m.group(1))

                # Fallback: find date directly in page (Amazon split label and date into separate elements)
                date_pattern = r'\d{1,2}\.\s+(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\s+\d{4}'
                for span in soup.find_all('span'):
                    text = span.get_text(strip=True)
                    m = re.match(date_pattern, text)
                    if m:
                        try:
                            return self.domain.parse_date(m.group(0))
                        except (ValueError, TypeError):
                            continue
                return None

            order_date = get_date(page_source, order_id)
            if order_date is None: 
                if self.dir_per_year:
                    raise ValueError(f'Failed to get date for order {order_id}')
                else:
                    # date is not necessary, so just log
                    logger.info(f'Failed to get date for order {order_id}')
            else:
                order_date = order_date.year
            invoice_path = self.get_invoice_path(order_date, order_id)
            if not os.path.exists(os.path.dirname(invoice_path)):
                os.makedirs(os.path.dirname(invoice_path))
            with atomic_write(
                    invoice_path, mode='w', encoding='utf-8',
                    newline='\n', overwrite=True) as f:
                # Write with Unicode Byte Order Mark to ensure content will be properly interpreted as UTF-8
                f.write('\ufeff' + page_source)
            logger.info('  Wrote %s', invoice_path)

    def get_monthly_invoices(self):
        """Save Amazon's monthly statement pages from /cpe/myinvoices.

        The listing page renders one row per month (year-grouped). Each row's
        Next.js-rendered anchor uses a client-side click handler instead of a
        real href, but the statement IDs (`amzn1.statement.sa.<base64>`) are
        embedded in the page HTML. We extract them by regex, then visit each
        `?statementId=<id>` detail URL and persist the rendered HTML — that
        detail page IS the printable invoice; the "Monatsabrechnung drucken"
        link just calls `window.print()` on it, there is no separate PDF.

        Files are written to `<output_directory>/monthly/<statementId>.html`.
        Re-runs skip statements already present on disk.
        """
        if not self.domain.monthly_invoices_path:
            logger.info(
                'Monthly invoices are not configured for amazon.%s',
                self.domain.top_level)
            return

        out_dir = os.path.join(self.output_directory, 'monthly')
        os.makedirs(out_dir, exist_ok=True)
        already_downloaded = {
            name[:-len('.html')] for name in os.listdir(out_dir)
            if name.endswith('.html')
        }

        base_url = 'https://www.amazon.' + self.domain.top_level
        list_url = base_url + self.domain.monthly_invoices_path
        logger.info('Navigating to monthly invoices page: %s', list_url)
        with self.wait_for_page_load():
            self.driver.get(list_url)

        statement_id_re = re.compile(r'amzn1\.statement\.sa\.[A-Za-z0-9]+')

        def find_statement_ids():
            ids = statement_id_re.findall(self.driver.page_source)
            if not ids:
                return None
            seen = set()
            ordered = []
            for sid in ids:
                if sid in seen:
                    continue
                seen.add(sid)
                ordered.append(sid)
            return ordered

        try:
            statement_ids, = self.wait_and_return(find_statement_ids,
                                                   timeout=30)
        except Exception:
            logger.warning(
                'No monthly statement IDs found at %s; the page may be '
                'empty or its structure may have changed.', list_url)
            return

        logger.info('Found %d monthly statement(s)', len(statement_ids))
        for statement_id in statement_ids:
            if statement_id in already_downloaded:
                logger.info('Already have statement %s, skipping',
                            statement_id)
                continue
            try:
                self._save_monthly_invoice(statement_id, out_dir, base_url)
                already_downloaded.add(statement_id)
            except Exception:
                logger.exception(
                    'Failed to download monthly invoice %s', statement_id)

    def _save_monthly_invoice(self, statement_id, out_dir, base_url):
        url = (base_url + self.domain.monthly_invoices_path +
               '?statementId=' + statement_id)
        logger.info('Fetching monthly invoice: %s', statement_id)
        with self.wait_for_page_load():
            self.driver.get(url)

        # Wait until the invoice details have actually rendered. Amazon's
        # pmts pages mark the section "Rechnungsdetails" / "Invoice Details"
        # and embed the statement ID once order data is loaded.
        def detail_ready():
            source = self.driver.page_source
            if statement_id not in source:
                return None
            return source

        page_source, = self.wait_and_return(detail_ready, timeout=60)

        out_path = os.path.join(out_dir, statement_id + '.html')
        with atomic_write(out_path, mode='w', encoding='utf-8',
                          newline='\n', overwrite=True) as f:
            # BOM keeps the file decoded as UTF-8 when opened in a browser.
            f.write('\ufeff' + page_source)
        logger.info('  Wrote %s', out_path)

    def get_gift_cards(self):
        """Save every page of the gift card balance / activity history.

        `/gc/balance` renders the running gift card ledger 15 rows to a page:
        "Gift Card added" (claim codes), "Gift Card applied to order",
        "Refund from order" (a refund on a gift-card-paid order goes back to
        the balance, not the card) and "Release of Gift Card Balance". Only
        the debits show up in order history, so this page is the sole source
        for the credit side.

        Paging is cursor-based (`?next=<base64>`) with no stable per-page
        identity — inserting one new row shifts every boundary — so unlike
        monthly invoices there is nothing to skip on re-runs. Each run walks
        the whole chain and rewrites `<output_directory>/giftcard/page-NNN.html`,
        clearing stale pages left by a longer previous run.
        """
        if not self.domain.gift_card_path:
            logger.info('Gift cards are not configured for amazon.%s',
                        self.domain.top_level)
            return

        out_dir = os.path.join(self.output_directory, 'giftcard')
        os.makedirs(out_dir, exist_ok=True)

        base_url = 'https://www.amazon.' + self.domain.top_level
        url = base_url + self.domain.gift_card_path
        logger.info('Navigating to gift card balance page: %s', url)

        written = []
        seen_urls = set()
        # ponytail: hard cap so a paging loop that never terminates cannot spin
        # forever; 200 pages is ~3000 rows, far beyond any real history.
        for page_num in range(1, 201):
            if url in seen_urls:
                logger.warning('Gift card paging revisited %s, stopping', url)
                break
            seen_urls.add(url)
            with self.wait_for_page_load():
                self.driver.get(url)

            page_source = self.driver.page_source
            out_path = os.path.join(out_dir, 'page-%03d.html' % page_num)
            with atomic_write(out_path, mode='w', encoding='utf-8',
                              newline='\n', overwrite=True) as f:
                # BOM keeps the file decoded as UTF-8 when opened in a browser.
                f.write('﻿' + page_source)
            written.append(out_path)
            logger.info('  Wrote %s', out_path)

            next_href = self._gift_card_next_href()
            if not next_href:
                break
            url = urllib.parse.urljoin(base_url, next_href)

        logger.info('Downloaded %d gift card page(s)', len(written))

        # Drop pages left over from an earlier, longer run so a stale tail is
        # not parsed as if it were current.
        keep = {os.path.basename(p) for p in written}
        for name in os.listdir(out_dir):
            if name.startswith('page-') and name.endswith('.html') \
                    and name not in keep:
                os.remove(os.path.join(out_dir, name))
                logger.info('  Removed stale %s', name)

    def _gift_card_next_href(self):
        """Return the href of the "Next" pager link, or None on the last page.

        The link text carries a trailing arrow ("Next→" / "Weiter→") depending
        on how the glyph is rendered, so match on the leading word rather than
        equality.
        """
        for link in self.driver.find_elements(By.TAG_NAME, 'a'):
            href = link.get_attribute('href')
            if not href or 'next=' not in href:
                continue
            text = (link.text or '').strip()
            if text.startswith('Next') or text.startswith(self.domain.next):
                return href
        return None

    def run(self):
        self.login()
        if not os.path.exists(self.output_directory):
            os.makedirs(self.output_directory)
        self.get_orders(
            regular=self.regular,
            digital_orders_menu=self.digital_orders_menu
            )
        if self.monthly_invoices:
            self.get_monthly_invoices()
        if self.gift_cards:
            self.get_gift_cards()


def run(**kwargs):
    scrape_lib.run_with_scraper(Scraper, **kwargs)


def interactive(**kwargs):
    return scrape_lib.interact_with_scraper(Scraper, **kwargs)
