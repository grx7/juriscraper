import email
import re
from datetime import date
from typing import Dict, List, Optional, TypedDict, Union

from lxml.html import HtmlElement

from ..lib.string_utils import clean_string, convert_date_string, harmonize
from .docket_report import BaseDocketReport
from .reports import BaseReport
from .utils import (
    get_pacer_case_id_from_doc1_url,
    get_pacer_doc_id_from_doc1_url,
    get_pacer_magic_num_from_doc1_url,
    get_pacer_seq_no_from_doc1_url,
)


class DocketEntryType(TypedDict):
    date_filed: date
    description: str
    document_number: Optional[str]
    document_url: Optional[str]
    pacer_case_id: Optional[str]
    pacer_doc_id: Optional[str]
    pacer_magic_num: Optional[str]
    pacer_seq_no: Optional[str]


class DocketType(TypedDict):
    case_name: str
    date_filed: date
    docket_entries: List[DocketEntryType]
    docket_number: str


class NotificationEmail(BaseDocketReport, BaseReport):
    """A BaseDocketReport for parsing PACER notification email parsing"""

    ERROR_STRINGS = ["Notice of Electronic Claims Filing"]

    def __init__(self, court_id):
        self.court_id = court_id
        self.content_type = None
        self.appellate = None
        self.docket_numbers = []
        super().__init__(court_id)

    @property
    def data(self):
        if self.is_valid is False or self.tree is None:
            return {}
        base = {
            "court_id": self.court_id,
        }
        if self.content_type == "text/plain":
            parsed = {
                "appellate": self._is_appellate(),
                "contains_attachments": self._contains_attachments_plain(),
                "dockets": self._get_dockets(),
                "email_recipients": self._get_email_recipients_plain(),
            }
        else:
            parsed = {
                "appellate": self._is_appellate(),
                "contains_attachments": self._contains_attachments(),
                "dockets": self._get_dockets(),
                "email_recipients": self._get_email_recipients(),
            }
        return {**base, **parsed}

    def _is_appellate(self) -> bool:
        """Gets the email notice type from the email text.

        :returns: True if is an NDA otherwise False
        """
        if self.appellate is None:
            if "Notice of Docket Activity" in self.tree.text_content():
                self.appellate = True
                return self.appellate
            self.appellate = False
            return self.appellate
        return self.appellate

    def _sibling_path(self, label):
        """Gets the path string for the sibling of a label cell (td)

        Many data attributes are presented in the format of key: value in table syntax
        this is a good way to get the next element over from a label

        :param label: The cell label
        :returns: The Xpath to the next cell
        """
        return f'.//td[contains(., "{label}:")]/following-sibling::td[1]'

    def _get_case_name(self, current_node: HtmlElement) -> str:
        """Gets a cleaned case name from the email text

        :param  current_node: The relative lxml.HtmlElement
        :returns: Case name, cleaned and harmonized
        """
        path = self._sibling_path("Case Name")
        case_name = self._xpath_text_0(current_node, path)
        if not case_name:
            case_name = self._xpath_text_0(current_node, f"{path}/p")
            if not case_name:
                case_name = "Unknown Case Title"
        return clean_string(harmonize(case_name))

    def _get_case_name_plain(self) -> str:
        """Gets a cleaned case name from the plain email text

        Raise an exception if a potential multi-docket text/plain notification
        is found so we can get the example and add support for it.

        :returns: Case name, cleaned and harmonized
        """
        email_body = self.tree.text_content()
        regex = r"Case Name:(.*)"
        find_case = re.findall(regex, email_body)
        if len(find_case) > 1:
            raise NotImplementedError(
                f"Received a potential multi-docket text/plain notification. "
                f"This is probably our chance to add support for it. "
                f"court: {self.court_id}"
            )
        if find_case:
            case_name = find_case[0]
        else:
            case_name = "Unknown Case Title"
        return clean_string(harmonize(case_name))

    def _get_docket_number(self, current_node: HtmlElement) -> str:
        """Gets a docket number from the email text

        :param  current_node: The relative lxml.HtmlElement
        :returns: Docket number, parsed
        """

        if self._is_appellate():
            path = self._sibling_path("Case Number")
            case_number = self._xpath_text_0(current_node, f"{path}/a")
            return case_number

        path = self._sibling_path("Case Number")
        docket_number = self._parse_docket_number_strs(
            current_node.xpath(f"{path}/a/text()")
        )
        if not docket_number:
            docket_number = self._parse_docket_number_strs(
                current_node.xpath(f"{path}/p/a/text()")
            )
        return docket_number

    def _get_docket_number_plain(self) -> str:
        """Gets a docket number from the plain email text

        :returns: Docket number, parsed
        """
        email_body = self.tree.text_content()
        regex = r"Case Number:(.*)"
        docket_number = re.findall(regex, email_body)
        return self._parse_docket_number_strs(docket_number)

    def _get_date_filed(self) -> date:
        """Gets the filing date from the email text

        :returns: Date filed as date object
        """
        date_filed = re.search(
            r"filed\son\s([\d|\/]*)", clean_string(self.tree.text_content())
        )
        return convert_date_string(
            date_filed[0].lower().replace("filed on ", "")
        )

    def _get_document_number(self, current_node: HtmlElement) -> str:
        """Gets the specific document number the notification is referring to

        :param  current_node: The relative lxml.HtmlElement
        :returns: Document number, cleaned
        """
        path = self._sibling_path("Document Number")
        node = current_node.xpath(path)[0].text_content()
        text_number = clean_string(node)
        if text_number == "No document attached":
            return None
        words = re.split(r"\(|\s", text_number)
        return words[0]

    def _get_document_number_plain(self) -> str:
        """Gets the specific document number the notification is referring to

        :returns: Document number, cleaned
        """
        email_body = self.tree.text_content()
        regex = r"Document Number:(.*)"
        document_number = re.findall(regex, email_body)
        if document_number:
            return clean_string(document_number[0])
        else:
            return None

    def _get_doc1_anchor(self, current_node: HtmlElement) -> str:
        """Safely retrieves the anchor tag for the document

        :param  current_node: The relative lxml.HtmlElement
        :returns: Anchor tag, if it's found
        """
        try:
            if self._is_appellate():
                path = f"{self._sibling_path('Document(s)')}//a"
            else:
                path = f"{self._sibling_path('Document Number')}//a"
            return current_node.xpath(path)[0]
        except IndexError:
            return None

    def _get_appellate_case_anchor(self) -> str:
        """Safely retrieves the anchor tag for the appellate case

        :returns: Anchor tag, if it's found
        """
        try:
            path = f"{self._sibling_path('Case Number')}//a"
            return self.tree.xpath(path)[0]
        except IndexError:
            return None

    def _get_appellate_pacer_case_id(self) -> str:
        """Extract the caseid from the appellate case anchor.

        :returns: caseID, if it's found
        """
        document_url = self._get_appellate_case_anchor().xpath("./@href")[0]
        match = re.search(r"caseId=(\d+)", document_url)
        if match:
            return match.group(1)
        else:
            return None

    def _get_description(self, current_node: HtmlElement) -> str:
        """Gets the docket text

        :param  current_node: The relative lxml.HtmlElement
        :returns: Cleaned docket text
        """

        description = ""
        # Paths to look for NEFs description
        main_path = './following::strong[contains(., "Docket Text:")][1]/following-sibling::'
        possible_paths = ["font[1]/b//text()", "b[1]/span//text()", "text()"]

        if self._is_appellate():
            # Paths to look for NDAs description
            main_path = './following::strong[contains(., "Docket Text:")][1]/following::'
            possible_paths = ["text()"]
        for path in possible_paths:
            node = current_node.xpath(f"{main_path}{path}")
            if len(node):
                for des_part in node:
                    if self._is_appellate():
                        if (
                            des_part
                            == "Notice will be electronically mailed to:"
                        ):
                            break
                    description = description + des_part
                description = clean_string(description)
                if description:
                    return description

        raise Exception(
            f"Can't get docket entry description, court: {self.court_id}"
        )

    def _get_description_plain(self):
        """Gets the docket text for plain email

        :returns: Cleaned docket text
        """
        email_body = self.tree.text_content()
        regex = r"^.*?Docket Text:(.*?)electronically mailed to:"
        find_description = re.findall(regex, email_body, re.DOTALL)
        description = ""
        if find_description:
            splitlines = find_description[0].splitlines()
            for index_line in range(len(splitlines)):
                if "Notice has been" not in splitlines[index_line]:
                    # Build description line by line
                    description = f"{description} {splitlines[index_line]}"
                else:
                    # Stop looking for description lines
                    break
        description = clean_string(description)
        if description:
            return description
        raise Exception(
            f"Can't get docket entry description for court: {self.court_id}"
        )

    def _contains_attachments(self) -> bool:
        """Determines if the html notification contains attached documents.

        :returns: True if it contains otherwise False.
        """

        document_nodes = self.tree.xpath(
            '//strong[contains(., "Document description:")]'
        )
        if len(document_nodes) <= 1:
            return False
        return True

    def _contains_attachments_plain(self) -> bool:
        """Determines if the plain/txt notification contains attached documents.

        :returns: True if it contains otherwise False.
        """

        mail_body = self.tree.text_content()
        regex = r"^.*?The following document\(s\) are associated with this transaction:(.*?)$"

        find_attachments = re.findall(regex, mail_body, re.DOTALL)
        associated_documents = 0
        if find_attachments:
            splitlines = find_attachments[0].splitlines()
            for index_line in range(len(splitlines)):
                if "Document description:" in splitlines[index_line]:
                    associated_documents = +1
        if associated_documents <= 1:
            return False
        return True

    def _get_dockets(self) -> DocketType:
        """Get all the dockets mentioned in the notification.

        Right now multiple docket notifications are only supported for text/html
        NEF notifications since we don't have examples for text/plain or NDAs
        that mention multiple dockets. When we get one we'll log an error
        and get the example.

        :return: DocketType Dict
        """
        dockets = []
        if self.content_type == "text/plain":
            docket = {
                "case_name": self._get_case_name_plain(),
                "docket_number": self._get_docket_number_plain(),
                "date_filed": self._get_date_filed(),
                "docket_entries": self._get_docket_entries(),
            }
            dockets.append(docket)
        else:
            dockets_table = self.tree.xpath(
                "//table[contains(., 'Case Name:')]"
            )
            if self.appellate and len(dockets_table) > 1:
                raise NotImplementedError(
                    f"Received a potential multi-docket NDA notification. "
                    f"This is probably our chance to add support for it. "
                    f"court: {self.court_id}"
                )
            for docket_table in dockets_table:
                docket_number = self._get_docket_number(docket_table)
                # Cache the docket number for its later use in
                # _get_email_recipients_with_links
                self.docket_numbers.append(docket_number)
                docket = {
                    "case_name": self._get_case_name(docket_table),
                    "docket_number": docket_number,
                    "date_filed": self._get_date_filed(),
                    "docket_entries": self._get_docket_entries(docket_table),
                }
                dockets.append(docket)
        return dockets

    def _get_docket_entries(
        self, current_node: HtmlElement = None
    ) -> List[DocketEntryType]:
        """Gets the full list of docket entries with document and sequence numbers

        :param  current_node: The relative lxml.HtmlElement
        :returns: List of docket entry dictionaries
        """
        if self.content_type == "text/plain":
            description = self._get_description_plain()
            if description is not None:
                email_body = self.tree.text_content()
                regex = r"view the document:[\r\n]+([^\r\n]+)"
                url = re.findall(regex, email_body)
                if url:
                    document_url = url[0]
                else:
                    document_url = None
                document_number = self._get_document_number_plain()
        else:
            description = self._get_description(current_node)
            if description is not None:
                anchor = self._get_doc1_anchor(current_node)
                document_url = (
                    anchor.xpath("./@href")[0] if anchor is not None else None
                )
                if self._is_appellate():
                    document_number = None
                else:
                    document_number = self._get_document_number(current_node)

        if description is not None:
            entries = [
                {
                    "date_filed": self._get_date_filed(),
                    "description": description,
                    "document_url": document_url,
                    "document_number": document_number,
                    "pacer_doc_id": None,
                    "pacer_case_id": None,
                    "pacer_seq_no": None,
                    "pacer_magic_num": None,
                }
            ]
            if document_url is not None:
                entries[0]["pacer_doc_id"] = get_pacer_doc_id_from_doc1_url(
                    document_url
                )
                entries[0][
                    "pacer_magic_num"
                ] = get_pacer_magic_num_from_doc1_url(
                    document_url, self.appellate
                )
                if not self._is_appellate():
                    entries[0][
                        "pacer_case_id"
                    ] = get_pacer_case_id_from_doc1_url(document_url)
                    entries[0][
                        "pacer_seq_no"
                    ] = get_pacer_seq_no_from_doc1_url(document_url)
            if self._is_appellate():
                entries[0][
                    "pacer_case_id"
                ] = self._get_appellate_pacer_case_id()
            return entries
        return []

    def _get_emaiL_recipients_without_links(self, recipient_lines):
        """Gets all the email recipients of the notification

        :returns: List of email recipients with names and email addresses
        """
        email_recipients = []
        for line in recipient_lines:
            if "@" in line:
                comma_separated = list(map(clean_string, line.split(",")))
                # The first element of comma_separated looks like "Stephen Breyer sbreyerguy52@hotmail.com"
                name_and_first_email = comma_separated[0].split(" ")
                # This re-joins so the name is by itself "Stephen Breyer"
                name = " ".join(name_and_first_email[:-1])
                # This is the leftover email in that first example "sbreyerguy52@hotmail.com"
                first_email = name_and_first_email[-1]
                # The remaining emails are the tail of the comma_separated list ["sbreyer@supremecourt.gov", "sbreyer@supremestreetwear.com"]
                other_emails = comma_separated[1:]
                email_recipients.append(
                    {
                        "name": name,
                        "email_addresses": [first_email] + other_emails,
                    }
                )
        return email_recipients

    def _get_email_recipients_with_links(self, text_content):
        """Gets all the email recipients of the notification if their emails are in links

        :returns: List of email recipients with names and email addresses
        """
        # Matching names in this format is a bit less reliable. May be worth coming back to.

        replacements = [
            (r"\n", ""),
            (
                r"\s{2,}|\t",
                " ",
            ),
            (
                r"\s,",
                "",
            ),
            (
                r"^.*mailed\sto:",
                "",
            ),
        ]
        for end_point in self.docket_numbers:
            replacements.append(
                (
                    f"{re.escape(end_point)}.*$",
                    "",
                )
            )

        for replacement in replacements:
            text_content = re.sub(replacement[0], replacement[1], text_content)

        recipient_parts = text_content.strip().split(" ")
        email_recipients = []
        for recipient_part in recipient_parts:
            if not len(email_recipients):
                email_recipients.append({"name": recipient_part})
            else:
                last_recipient = email_recipients[-1]
                if "@" in recipient_part:
                    if not last_recipient.get("email_addresses"):
                        last_recipient["email_addresses"] = []
                    last_recipient["email_addresses"].append(
                        re.sub(r",", "", recipient_part)
                    )
                elif last_recipient.get("email_addresses") and len(
                    last_recipient["email_addresses"]
                ):
                    email_recipients.append({"name": recipient_part})
                else:
                    last_recipient["name"] += f" {recipient_part}"
        return list(
            filter(
                lambda recipient: recipient.get("email_addresses", False)
                and len(recipient.get("email_addresses")) > 0,
                email_recipients,
            )
        )

    def _get_email_recipients(self) -> List[Dict[str, Union[str, List[str]]]]:
        """Gets all the email recipients whether they come from plain text or more HTML formatting

        :returns: List of email recipients with names and email addresses
        """
        if self._is_appellate():
            path = '//strong[contains(., "Notice will be electronically mailed to")]/following-sibling::'
            recipient_lines = self.tree.xpath(f"{path}text()")
        else:
            path = '//b[contains(., "Notice has been electronically mailed to")]/following-sibling::'
            recipient_lines = self.tree.xpath(f"{path}text()")
            link_lines = self.tree.xpath(f"{path}a")
            if len(link_lines):
                return self._get_email_recipients_with_links(
                    self.tree.xpath(
                        'string(//b[contains(., "Notice has been electronically mailed to")]/parent::node())'
                    )
                )
        return self._get_email_recipients_with_links(" ".join(recipient_lines))

    def _get_email_recipients_plain(
        self,
    ) -> List[Dict[str, Union[str, List[str]]]]:
        """Gets all the email recipients whether they come from plain text or more HTML formatting

        :returns: List of email recipients with names and email addresses
        """
        email_recipients = []
        mail_body = self.tree.text_content()
        regex = r"^.*?Notice has been electronically mailed to:(.*?)$"

        # Return all lines after recipients begins
        find_emails = re.findall(regex, mail_body, re.DOTALL)
        if find_emails:
            email_lines = find_emails[0]
            splitlines = email_lines.splitlines()
            for index_line in range(len(splitlines)):
                if "@" in splitlines[index_line]:
                    email_separated = list(
                        map(clean_string, splitlines[index_line].split(","))
                    )
                    # Obtains comma separated email addresses ["sbreyer@supremecourt.gov", "sbreyer@supremestreetwear.com"]
                    name = clean_string(splitlines[index_line - 1])
                    # Obtains recipient name
                    email_recipients.append(
                        {
                            "email_addresses": email_separated,
                            "name": name,
                        }
                    )
                if "Notice will be delivered" in splitlines[index_line]:
                    # Stop looking for email addresses
                    break
        return email_recipients


class S3NotificationEmail(NotificationEmail):
    """A subclass of the NotificationEmail report. This handles all the S3 specific format issues that come from
    SES emails automatically archived in S3.
    """

    def _combine_lines_with_proper_spaces(self, text):
        """Re-composes S3 line breaks to have proper spacing depending on line ending character

        :returns: String with spacing as read normally
        """
        lines = text.split("\n")
        combined = ""
        last_line_match = False
        for line in lines:
            match = re.search(r"=$", line)
            if match:
                combined += re.sub(r"=$", "", line)
                last_line_match = True
            elif last_line_match:
                combined += line
                last_line_match = False
            else:
                combined += f" {line}"
        return combined

    def _html_from_s3_email(self, text):
        """Pulls the HTML content, parsed with line breaks normalized for from the S3 email file

        :returns: String with proper replacements for normal HTML parsing
        """
        # Remove line ends form S3 content
        cleaned_s3_line_ends = self._combine_lines_with_proper_spaces(text)
        return cleaned_s3_line_ends

    def _parse_text(self, text):
        """MIME Parser from file text, text/html and text/plain messages are supported.
        This obtains the email payload decoded as UTF-8.
        """
        message = email.message_from_string(text)
        if message.is_multipart():
            for part in message.walk():
                c_type = part.get_content_type()
                # If multipart message, parse text/html message
                if c_type == "text/html":
                    body = part.get_payload(decode=True)  # decode
                    self.content_type = "text/html"
                    break
        else:
            # If not multipart, parse either text/html or text/plain message
            for part in message.walk():
                c_type = part.get_content_type()
                c_dispo = str(part.get("Content-Disposition"))

                if c_type == "text/html":
                    body = part.get_payload(decode=True)
                    self.content_type = "text/html"
                    break

                elif c_type == "text/plain" and "attachment" not in c_dispo:
                    body = message.get_payload(decode=True)
                    self.content_type = "text/plain"
                    break

        try:
            # Try to decode email body using utf-8
            email_body = body.decode("utf-8")
        except UnicodeDecodeError:
            # If it fails fallback on iso-8859-1
            email_body = body.decode("iso-8859-1")
        if self.content_type == "text/plain":
            return super()._parse_text(email_body)
        elif self.content_type == "text/html":
            html_only = self._html_from_s3_email(email_body)
            return super()._parse_text(html_only)
