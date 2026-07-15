import dayjs from "dayjs";
import updateLocale from "dayjs/plugin/updateLocale";
import "dayjs/locale/zh-cn";

dayjs.extend(updateLocale);

const NUMERIC_MONTHS = [
  "1 月",
  "2 月",
  "3 月",
  "4 月",
  "5 月",
  "6 月",
  "7 月",
  "8 月",
  "9 月",
  "10 月",
  "11 月",
  "12 月",
] as const;

dayjs.updateLocale("zh-cn", {
  months: [...NUMERIC_MONTHS],
  monthsShort: [...NUMERIC_MONTHS],
});
